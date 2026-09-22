"""Bounded A/B write-and-restore coordinator; no live CLI or credential factory.

The trusted application must supply a native lifecycle and a qualified secret
capability. Tests patch HTTPS and use only generated content. This component
does not by itself qualify enrollment, refresh, session gating, or C creation.
"""
from dataclasses import asdict
import json
import time

from .broker import RequestIntent
from .docs_plan import EditPlan, canonical, digest, parse_document, rollback
from .policy import Denied, require
from .provider import GoogleProvider, validate_write
from .recovery import RecoveryReceipt, RecoveryStore
from .stage import StageLifecycle
from .transactions import (RESTORE_BUCKET, require_file_available, spendable_totals,
                           validate_transactions)


class WriteExecutor:
    def __init__(self, lifecycle, binding, recovery, provider, secrets, token_ref, *, clock=time.time):
        require(type(lifecycle) is StageLifecycle and type(recovery) is RecoveryStore
                and type(provider) is GoogleProvider and provider.binding == binding
                and recovery.binding_hash == digest(canonical(asdict(binding)))
                and recovery.protector.sid == binding.sid
                and callable(getattr(secrets, 'business_token', None))
                and type(token_ref) is str and bool(token_ref), 'EXECUTOR_CONFIGURATION_DENIED')
        self.stage, self.binding, self.recovery = lifecycle, binding, recovery
        self.provider, self.secrets, self.token_ref, self.clock = provider, secrets, token_ref, clock

    def _now(self):
        return int(self.clock())

    def _active(self):
        self.stage.assert_active(self.binding, now=self._now())

    def _call(self, action, value):
        # The stage lock remains held through secret access and the one HTTP call.
        self._active()
        token = self.secrets.business_token(self.token_ref)
        return getattr(self.provider, action)(token, value)

    def _request(self, data, key, *, lane='normal', bucket='A_B_flow', file=None,
                 rollback_write=False, package=None, pin=None):
        self.stage._new_operation(data, key)
        self.stage._check_request(spendable_totals(data), lane, bucket, file,
                                  rollback_write, False, False, package)
        intent = data['operations'][pin] if pin else None
        data['operations'][key] = {
            'kind': 'REQUEST', 'generation': self.binding.generation, 'lane': lane, 'bucket': bucket,
            'file': file, 'rollback': rollback_write, 'create': False, 'refresh': False,
            'retest_package': package, 'cleanup': False, 'intent_ref': pin,
            'intent_hash': intent['intent_hash'] if intent else None,
            'intent_resource': intent['resource'] if intent else None,
            'intent_revision': intent['revision'] if intent else None,
            'sequence': len(data['operations']) + 1}
        data['totals'] = self.stage._rebuild_totals(data['operations'])

    def _pin(self, data, key, plan, receipt):
        self.stage._new_operation(data, key)
        intent = RequestIntent('POST', 'docs.batchUpdate', plan.before.document_id,
                               plan.request_hash, plan.before.revision)
        data['operations'][key] = {
            'kind': 'WRITE_INTENT', 'generation': self.binding.generation,
            'resource': plan.before.document_id, 'revision': plan.before.revision,
            'intent_hash': intent.intent_hash, 'recovery_hash': digest(canonical(asdict(receipt))),
            'sequence': len(data['operations']) + 1}
        return intent.intent_hash

    def _commit(self, data):
        # Validate everything before the durable store is touched, not afterwards.
        data['totals'] = self.stage._rebuild_totals(data['operations'])
        validate_transactions(data)
        held = spendable_totals(data)
        limits = self.stage.limits
        require(held['api_total'] <= limits.api_total and held['api_safety'] <= limits.api_safety
                and held['buckets'][RESTORE_BUCKET] <= limits.buckets('safety')[RESTORE_BUCKET]
                and all(held['writes'][f] <= getattr(limits, 'writes_' + f.lower())
                        and held['rollbacks'][f] <= getattr(limits, 'rollbacks_' + f.lower()) for f in ('A', 'B'))
                and all(r['api'] <= 20 and r['rollback'] <= 1 for r in held['retest'].values()),
                'RESTORE_RESERVE_EXHAUSTED')
        self.stage._save(data)

    def _save_plan(self, key, plan):
        raw = canonical({'binding_hash': self.recovery.binding_hash,
                         'before': json.loads(plan.before.raw), 'after_text': plan.after_text,
                         'request': json.loads(plan.request_bytes)})
        receipt = self.recovery.save(key, raw)
        require(self.recovery.load(receipt) == raw, 'RECOVERY_READBACK_FAILED')
        return receipt

    def _load_plan(self, tx):
        package = json.loads(self.recovery.load(RecoveryReceipt(**tx['receipt'])))
        require(type(package) is dict and set(package) == {'binding_hash', 'before', 'after_text', 'request'}
                and package['binding_hash'] == self.recovery.binding_hash, 'RECOVERY_PACKAGE_INVALID')
        before = parse_document(package['before'], tx['resource'])
        plan = EditPlan(before, package['after_text'], canonical(package['request']))
        validate_write(plan, {self.binding.a_id, self.binding.b_id})
        intent = RequestIntent('POST', 'docs.batchUpdate', tx['resource'], plan.request_hash, before.revision)
        require(intent.intent_hash == tx['intent_hash'] and before.revision == tx['revision'],
                'RECOVERY_PACKAGE_INVALID')
        return plan

    def _transaction(self, key):
        data = self.stage._load()
        self.stage._authorize(data, self.binding, self._now())
        tx = data['transactions'].get(key)
        require(type(tx) is dict and tx['generation'] == self.binding.generation, 'TRANSACTION_DENIED')
        return data, tx

    def _phase(self, key, phase):
        data, tx = self._transaction(key)
        tx['phase'] = phase
        if phase == 'RESTORED':
            tx['held_api'] = tx['held_write'] = 0
        self._commit(data)

    def write(self, key, plan, *, retest_package=None):
        require(type(key) is str and 1 <= len(key) <= 80
                and all(c.isascii() and (c.isalnum() or c in '-_') for c in key), 'OPERATION_INVALID')
        validate_write(plan, {self.binding.a_id, self.binding.b_id})
        file = 'A' if plan.before.document_id == self.binding.a_id else 'B'
        with self.stage.lock:
            self._active()
            data = self.stage._load()
            require(key not in data['transactions'], 'OPERATION_REPLAY')
            require_file_available(data, file)
            # Both preflight reads are charged atomically before either is sent.
            for suffix in ('before-meta', 'before-doc'):
                self._request(data, key + '.' + suffix, package=retest_package)
            self._commit(data)
            self._call('metadata', plan.before.document_id)
            current = self._call('document', plan.before.document_id)
            require(current == plan.before, 'SOURCE_CHANGED')
            receipt = self._save_plan(key, plan)
            data = self.stage._load()
            intent_hash = self._pin(data, key + '.pin', plan, receipt)
            self._request(data, key + '.write', file=file, package=retest_package, pin=key + '.pin')
            for suffix in ('after-meta', 'after-doc'):
                self._request(data, key + '.' + suffix, package=retest_package)
            data['transactions'][key] = {
                'generation': self.binding.generation, 'file': file, 'resource': plan.before.document_id,
                'receipt': asdict(receipt), 'intent_hash': intent_hash, 'revision': plan.before.revision,
                'phase': 'WRITING', 'held_api': 5, 'held_write': 1, 'retest_package': retest_package,
                'restore_receipt': None}
            self._commit(data)
            # No public resume-dispatch entry point: persisted WRITING is uncertain.
            try:
                self._call('write_document', plan)
                self._call('metadata', plan.before.document_id)
                plan.verify(self._call('document', plan.before.document_id))
                self._phase(key, 'VERIFIED')
            except Exception:
                self._phase(key, 'UNKNOWN')
                raise Denied('WRITE_OUTCOME_UNKNOWN') from None
            return {'operation_id': key, 'status': 'VERIFIED'}

    def restore(self, key):
        with self.stage.lock:
            data, tx = self._transaction(key)
            require(tx['phase'] == 'VERIFIED' and tx['restore_receipt'] is None
                    and tx['held_api'] == 5 and tx['held_write'] == 1, 'RESTORE_DENIED')
            plan = self._load_plan(tx)
            # Consume existing holds; do not allocate a second budget pool.
            tx['phase'], tx['held_api'] = 'RESTORING', 3
            for suffix in ('before-meta', 'before-doc'):
                self._request(data, key + '.restore.' + suffix, lane='safety', bucket=RESTORE_BUCKET,
                              package=tx['retest_package'])
            self._commit(data)
            try:
                self._call('metadata', tx['resource'])
                current = self._call('document', tx['resource'])
                inverse = rollback(plan, current)
                validate_write(inverse, {tx['resource']})
                receipt = self._save_plan(key + '.restore', inverse)
                data, tx = self._transaction(key)
                tx['held_api'] = tx['held_write'] = 0
                tx['restore_receipt'] = asdict(receipt)
                self._pin(data, key + '.restore.pin', inverse, receipt)
                self._request(data, key + '.restore.write', lane='safety', bucket=RESTORE_BUCKET,
                              file=tx['file'], rollback_write=True, package=tx['retest_package'],
                              pin=key + '.restore.pin')
                for suffix in ('after-meta', 'after-doc'):
                    self._request(data, key + '.restore.' + suffix, lane='safety', bucket=RESTORE_BUCKET,
                                  package=tx['retest_package'])
                self._commit(data)
                self._call('write_document', inverse)
                self._call('metadata', tx['resource'])
                inverse.verify(self._call('document', tx['resource']))
                self._phase(key, 'RESTORED')
            except Exception:
                self._phase(key, 'UNKNOWN')
                raise Denied('RESTORE_OUTCOME_UNKNOWN') from None
            return {'operation_id': key, 'status': 'RESTORED'}

    def reconcile(self, key, probe_id):
        """An explicit, counted exact-resource read; it never resends a write."""
        require(type(probe_id) is str and 1 <= len(probe_id) <= 40
                and all(c.isascii() and (c.isalnum() or c in '-_') for c in probe_id), 'OPERATION_INVALID')
        with self.stage.lock:
            data, tx = self._transaction(key)
            require(tx['phase'] in {'WRITING', 'RESTORING', 'UNKNOWN'}, 'RECONCILE_DENIED')
            plan = self._load_plan(tx)
            for suffix in ('meta', 'doc'):
                self._request(data, key + '.probe-' + probe_id + '.' + suffix, lane='safety',
                              bucket='exact_unknown_reconciliation', package=tx['retest_package'])
            self._commit(data)
            try:
                self._call('metadata', tx['resource'])
                current = self._call('document', tx['resource'])
                require(current.document_id == plan.before.document_id and current.tab_id == plan.before.tab_id
                        and current.style_hash == plan.before.style_hash, 'READBACK_MISMATCH')
                if current.text == plan.before.text:
                    phase = 'RESTORED'
                elif current.text == plan.after_text and tx['held_api'] == 5 and tx['held_write'] == 1:
                    phase = 'VERIFIED'
                else:
                    phase = 'UNKNOWN'
                self._phase(key, phase)
            except Exception:
                self._phase(key, 'UNKNOWN')
                raise Denied('RECONCILE_OUTCOME_UNKNOWN') from None
            return {'operation_id': key, 'status': phase}
