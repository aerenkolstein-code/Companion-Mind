"""Stage-wide, generation-safe S3 lifecycle accounting.

This is local policy only.  It has no OAuth client, HTTP transport, or secret
access.  A durable store (for example :class:`NativeLedger`) supplies the
cross-process lock and fail-closed persistence boundary.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from threading import RLock

from .policy import Denied, Limits, require


SCOPE = 'drive.file'
FORMAT = 'connector-s3-stage/1'
RETEST_PACKAGES = (1, 2)
MAX_GENERATIONS = 4


def _strings(values):
    return all(type(value) is str and 1 <= len(value) <= 180 and value.strip() == value
               for value in values)


@dataclass(frozen=True)
class GenerationBinding:
    """All non-secret dimensions that a generation must bind exactly."""
    generation: int
    account: str
    subject: str
    universe: str
    client_id: str
    project_id: str
    scope: str
    sid: str
    issued_at: int
    expires_at: int
    folder_id: str
    a_id: str
    b_id: str

    def __post_init__(self):
        require(type(self.generation) is int and self.generation > 0, 'GENERATION_INVALID')
        require(_strings((self.account, self.subject, self.universe, self.client_id,
                          self.project_id, self.scope, self.sid, self.folder_id,
                          self.a_id, self.b_id)), 'BINDING_INVALID')
        require(self.scope == SCOPE and type(self.issued_at) is int and type(self.expires_at) is int
                and self.issued_at >= 0 and self.issued_at < self.expires_at
                and self.expires_at - self.issued_at <= 72 * 60 * 60,
                'BINDING_INVALID')
        require(len({self.folder_id, self.a_id, self.b_id}) == 3, 'BINDING_INVALID')

    def stage_key(self):
        """Immutable scope of a stage; expiry and generation may legitimately change."""
        data = asdict(self)
        del data['generation']
        del data['issued_at']
        del data['expires_at']
        return data


class StageLifecycle:
    """One monotonic stage ledger shared by all of its generations."""
    def __init__(self, binding, store, limits=Limits()):
        require(isinstance(binding, GenerationBinding), 'BINDING_INVALID')
        self.binding, self.store, self.limits = binding, store, limits
        self.lock = getattr(store, 'lock', RLock())

    def initialize(self):
        with self.lock:
            require(self.binding.generation == 1, 'GENERATION_INITIALIZATION_DENIED')
            data = self._initial(self.binding)
            if hasattr(self.store, 'initialize'):
                self.store.initialize(data)
            else:
                require(self.store.read() is None, 'LEDGER_ALREADY_EXISTS')
                self.store.write(data)

    def start_next_generation(self, binding):
        """Open precisely the next generation after the current one is closed."""
        require(isinstance(binding, GenerationBinding), 'BINDING_INVALID')
        with self.lock:
            data = self._load()
            current = data['generations'][str(data['current_generation'])]
            require(not data['revoked'], 'GRANT_REVOKED')
            require(current['status'] == 'CLOSED', 'GENERATION_NOT_CLOSED')
            require(binding.generation <= MAX_GENERATIONS
                    and binding.generation == data['current_generation'] + 1
                    and binding.stage_key() == data['stage'], 'GENERATION_SCOPE_EXPANSION')
            data['current_generation'] = binding.generation
            data['generations'][str(binding.generation)] = {
                'binding': asdict(binding), 'status': 'ACTIVE'}
            self._save(data)

    def close_generation(self, generation):
        with self.lock:
            data = self._load()
            record = self._active(data, generation)
            record['status'] = 'CLOSED'
            self._save(data)

    def revoke_local(self, binding):
        """Persistently close the active generation before any remote cleanup."""
        with self.lock:
            data = self._load()
            self._bound_current(data, binding, active=True)
            data['generations'][str(binding.generation)]['status'] = 'CLOSED'
            data['revoked'] = True
            self._save(data)

    def begin_manual_reconnect(self, binding):
        """Create only the next consent-pending generation; never revive the old one."""
        require(isinstance(binding, GenerationBinding), 'BINDING_INVALID')
        with self.lock:
            data = self._load()
            current = data['generations'][str(data['current_generation'])]
            require(data['revoked'] and current['status'] == 'CLOSED', 'MANUAL_RECONNECT_DENIED')
            require(binding.generation <= MAX_GENERATIONS
                    and binding.generation == data['current_generation'] + 1
                    and binding.stage_key() == data['stage'], 'GENERATION_SCOPE_EXPANSION')
            data['current_generation'] = binding.generation
            data['generations'][str(binding.generation)] = {'binding': asdict(binding), 'status': 'CONSENT_REQUIRED'}
            data['revoked'] = False
            self._save(data)

    def activate_after_verified_consent(self, binding, proof_hash):
        """Only a later native identity/Picker verifier may call this transition."""
        require(type(proof_hash) is str and len(proof_hash) == 64 and all(c in '0123456789abcdef' for c in proof_hash),
                'CONSENT_PROOF_DENIED')
        with self.lock:
            data = self._load()
            self._bound_current(data, binding, active=False, consent=True)
            data['generations'][str(binding.generation)]['status'] = 'ACTIVE'
            self._save(data)

    def start_oauth(self, binding, operation_id, *, now, reconnect=None, retest_package=None):
        """Pre-charge an OAuth start; reconnects and retests are stage cumulative."""
        with self.lock:
            data = self._load()
            self._authorize_oauth(data, binding, now)
            self._new_operation(data, operation_id)
            inferred_reconnect = any(op['kind'] == 'OAUTH' for op in data['operations'].values())
            require(reconnect in {None, inferred_reconnect}, 'RECONNECT_CLASSIFICATION_DENIED')
            self._check_oauth(data['totals'], inferred_reconnect, retest_package)
            data['operations'][operation_id] = {'kind': 'OAUTH', 'generation': binding.generation,
                                                'reconnect': inferred_reconnect,
                                                'retest_package': retest_package,
                                                'sequence': len(data['operations']) + 1}
            data['totals'] = self._rebuild_totals(data['operations'])
            self._save(data)

    def reserve_request(self, binding, operation_id, *, now, lane, bucket, file=None,
                        rollback=False, create=False, refresh=False, retest_package=None, intent_ref=None):
        """Pre-charge a synthetic provider request against all applicable caps."""
        with self.lock:
            data = self._load()
            self._authorize(data, binding, now)
            self._new_operation(data, operation_id)
            require(lane in {'normal', 'safety'}, 'LANE_INVALID')
            caps = self.limits.buckets(lane)
            require(bucket in caps and file in {None, 'A', 'B', 'C'}, 'REQUEST_INVALID')
            require(type(rollback) is bool and type(create) is bool and type(refresh) is bool,
                    'REQUEST_INVALID')
            require(not create or (file == 'C' and not rollback), 'CREATE_DENIED')
            require(intent_ref is None or (type(intent_ref) is str and intent_ref in data['operations']), 'INTENT_PIN_REQUIRED')
            self._check_request(data['totals'], lane, bucket, file, rollback, create, refresh, retest_package)
            data['operations'][operation_id] = {
                'kind': 'REQUEST', 'generation': binding.generation, 'lane': lane,
                'bucket': bucket, 'file': file, 'rollback': rollback, 'create': create,
                'refresh': refresh, 'retest_package': retest_package, 'cleanup': False,
                'intent_ref': intent_ref,
                'sequence': len(data['operations']) + 1}
            data['totals'] = self._rebuild_totals(data['operations'])
            self._save(data)

    def pin_write_intent(self, binding, pin_id, *, now, resource, revision, intent_hash, recovery_hash):
        """Persist the non-secret recovery and exact write commitment before reserve."""
        with self.lock:
            data = self._load(); self._authorize(data, binding, now); self._new_operation(data, pin_id)
            require(type(resource) is str and resource in {binding.a_id, binding.b_id}
                    and type(revision) is str and bool(revision)
                    and all(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
                            for value in (intent_hash, recovery_hash)), 'INTENT_PIN_DENIED')
            data['operations'][pin_id] = {'kind': 'WRITE_INTENT', 'generation': binding.generation,
                'resource': resource, 'revision': revision, 'intent_hash': intent_hash,
                'recovery_hash': recovery_hash, 'sequence': len(data['operations']) + 1}
            data['totals'] = self._rebuild_totals(data['operations']); self._save(data)

    def reserve_cleanup(self, binding, operation_id, *, now):
        """The only post-revoke request: fixed safety/revoke, no refresh or file."""
        with self.lock:
            data = self._load()
            self._cleanup_authorize(data, binding, now)
            self._new_operation(data, operation_id)
            self._check_request(data['totals'], 'safety', 'revoke', None, False, False, False, None)
            data['operations'][operation_id] = {
                'kind': 'REQUEST', 'generation': binding.generation, 'lane': 'safety',
                'bucket': 'revoke', 'file': None, 'rollback': False, 'create': False,
                'refresh': False, 'retest_package': None, 'cleanup': True, 'intent_ref': None,
                'sequence': len(data['operations']) + 1}
            data['totals'] = self._rebuild_totals(data['operations'])
            self._save(data)

    def snapshot(self):
        with self.lock:
            return self._load()

    def _authorize(self, data, binding, now):
        require(isinstance(binding, GenerationBinding) and type(now) is int, 'BINDING_INVALID')
        require(not data['revoked'], 'GRANT_REVOKED')
        self._bound_current(data, binding, active=True)
        require(binding.issued_at <= now < binding.expires_at, 'GENERATION_EXPIRED')

    def _authorize_oauth(self, data, binding, now):
        require(isinstance(binding, GenerationBinding) and type(now) is int and not data['revoked'], 'GRANT_REVOKED')
        require(binding.stage_key() == data['stage'] and binding.generation == data['current_generation'],
                'GENERATION_CLOSED')
        record = data['generations'].get(str(binding.generation))
        require(type(record) is dict and record.get('binding') == asdict(binding), 'GENERATION_BINDING_MISMATCH')
        require(record.get('status') in {'ACTIVE', 'CONSENT_REQUIRED'}, 'GENERATION_CLOSED')
        require(binding.issued_at <= now < binding.expires_at, 'GENERATION_EXPIRED')

    def _cleanup_authorize(self, data, binding, now):
        require(isinstance(binding, GenerationBinding) and type(now) is int and data['revoked'],
                'CLEANUP_DENIED')
        self._bound_current(data, binding, active=False)
        require(now >= binding.issued_at, 'CLEANUP_DENIED')

    def _bound_current(self, data, binding, *, active, consent=False):
        require(isinstance(binding, GenerationBinding) and binding.stage_key() == data['stage'],
                'GENERATION_SCOPE_EXPANSION')
        require(type(binding.generation) is int and binding.generation == data['current_generation'],
                'GENERATION_CLOSED')
        record = data['generations'].get(str(binding.generation))
        expected = 'ACTIVE' if active else ('CONSENT_REQUIRED' if consent else 'CLOSED')
        require(type(record) is dict and record.get('binding') == asdict(binding),
                'GENERATION_BINDING_MISMATCH')
        require(record.get('status') == expected, 'GENERATION_CLOSED' if active else 'CLEANUP_DENIED')
        return record

    def assert_active(self, binding, *, now):
        with self.lock:
            self._authorize(self._load(), binding, now)

    def assert_cleanup(self, binding, *, now):
        with self.lock:
            self._cleanup_authorize(self._load(), binding, now)

    def _active(self, data, generation):
        require(type(generation) is int and generation == data['current_generation'],
                'GENERATION_CLOSED')
        record = data['generations'].get(str(generation))
        require(type(record) is dict and record.get('status') == 'ACTIVE', 'GENERATION_CLOSED')
        return record

    def _new_operation(self, data, operation_id):
        require(type(operation_id) is str and 1 <= len(operation_id) <= 128
                and operation_id not in data['operations'], 'OPERATION_REPLAY')

    def _retest(self, totals, package, *, api=0, oauth=0, rollback=0, nonrollback=0):
        if package is None:
            return
        require(type(package) is int and package in RETEST_PACKAGES, 'RETEST_PACKAGE_DENIED')
        record = totals['retest'][str(package)]
        require(record['api'] + api <= 20 and record['oauth'] + oauth <= 1
                and record['rollback'] + rollback <= 1
                and record['nonrollback'] + nonrollback <= 2, 'RETEST_PACKAGE_EXHAUSTED')
        record['api'] += api
        record['oauth'] += oauth
        record['rollback'] += rollback
        record['nonrollback'] += nonrollback

    def _check_oauth(self, totals, reconnect, retest_package):
        require(totals['oauth'] < self.limits.oauth, 'OAUTH_BUDGET_EXHAUSTED')
        require(not reconnect or totals['reconnect'] < 3, 'RECONNECT_BUDGET_EXHAUSTED')
        self._retest(copy.deepcopy(totals), retest_package, oauth=1)

    def _check_request(self, totals, lane, bucket, file, rollback, create, refresh, retest_package):
        caps = self.limits.buckets(lane)
        require(totals['api_total'] < self.limits.api_total, 'API_BUDGET_EXHAUSTED')
        require(totals['api_' + lane] < getattr(self.limits, 'api_' + lane), 'API_LANE_EXHAUSTED')
        require(totals['buckets'][bucket] < caps[bucket], 'API_BUCKET_EXHAUSTED')
        require(not refresh or totals['refresh'] < self.limits.refresh, 'REFRESH_BUDGET_EXHAUSTED')
        if file:
            require(totals['writes'][file] < getattr(self.limits, 'writes_' + file.lower()),
                    'WRITE_BUDGET_EXHAUSTED')
            if rollback:
                require(totals['rollbacks'][file] < getattr(self.limits, 'rollbacks_' + file.lower()),
                        'ROLLBACK_BUDGET_EXHAUSTED')
            else:
                require(totals['nonrollbacks'][file] < getattr(self.limits, 'nonrollbacks_' + file.lower()),
                        'NONROLLBACK_BUDGET_EXHAUSTED')
            require(not create or totals['creates_c'] < self.limits.creates_c,
                    'CREATE_BUDGET_EXHAUSTED')
        self._retest(copy.deepcopy(totals), retest_package, api=1,
                     rollback=int(rollback and bool(file)), nonrollback=int((not rollback) and bool(file)))

    def _initial(self, binding):
        return {'format': FORMAT, 'stage': binding.stage_key(), 'current_generation': binding.generation,
                'generations': {str(binding.generation): {'binding': asdict(binding), 'status': 'ACTIVE'}},
                'revoked': False, 'totals': self._totals(), 'operations': {}}

    def _totals(self):
        return {'oauth': 0, 'reconnect': 0, 'refresh': 0, 'api_total': 0, 'api_normal': 0,
                'api_safety': 0, 'writes': {'A': 0, 'B': 0, 'C': 0},
                'rollbacks': {'A': 0, 'B': 0, 'C': 0},
                'nonrollbacks': {'A': 0, 'B': 0, 'C': 0}, 'creates_c': 0,
                'buckets': {k: 0 for k in dict(self.limits.ordinary_buckets + self.limits.safety_buckets)},
                'retest': {str(p): {'api': 0, 'oauth': 0, 'rollback': 0, 'nonrollback': 0}
                           for p in RETEST_PACKAGES}}

    def _load(self):
        try:
            data = copy.deepcopy(self.store.read())
        except (TypeError, ValueError):
            raise Denied('RECOVERY_REQUIRED') from None
        require(type(data) is dict and set(data) == {'format', 'stage', 'current_generation', 'generations', 'revoked', 'totals', 'operations'}
                and data.get('format') == FORMAT and data.get('stage') == self.binding.stage_key(),
                'RECOVERY_REQUIRED')
        require(type(data['current_generation']) is int and 1 <= data['current_generation'] <= MAX_GENERATIONS
                and type(data['generations']) is dict and type(data['totals']) is dict
                and type(data['operations']) is dict and type(data['revoked']) is bool, 'RECOVERY_REQUIRED')
        expected_generations = {str(i) for i in range(1, data['current_generation'] + 1)}
        require(set(data['generations']) == expected_generations, 'RECOVERY_REQUIRED')
        for number in range(1, data['current_generation'] + 1):
            record = data['generations'][str(number)]
            require(type(record) is dict and set(record) == {'binding', 'status'} and type(record['binding']) is dict,
                    'RECOVERY_REQUIRED')
            try:
                bound = GenerationBinding(**record['binding'])
            except (Denied, TypeError):
                raise Denied('RECOVERY_REQUIRED') from None
            expected_statuses = {'ACTIVE', 'CLOSED', 'CONSENT_REQUIRED'} if number == data['current_generation'] else {'CLOSED'}
            require(bound.generation == number and bound.stage_key() == data['stage']
                    and type(record['status']) is str and record['status'] in expected_statuses,
                    'RECOVERY_REQUIRED')
        require(data['totals'] == self._rebuild_totals(data['operations']), 'RECOVERY_REQUIRED')
        return data

    def _rebuild_totals(self, operations):
        require(type(operations) is dict, 'RECOVERY_REQUIRED')
        totals = self._totals()
        records = []
        for key, op in operations.items():
            require(type(key) is str and 1 <= len(key) <= 128 and type(op) is dict
                    and type(op.get('kind')) is str and type(op.get('generation')) is int
                    and 1 <= op['generation'] <= MAX_GENERATIONS and type(op.get('sequence')) is int
                    and 1 <= op['sequence'] <= len(operations), 'RECOVERY_REQUIRED')
            records.append((key, op))
        require({op['sequence'] for _, op in records} == set(range(1, len(records) + 1)), 'RECOVERY_REQUIRED')
        pins = {}
        for key, op in sorted(records, key=lambda record: record[1]['sequence']):
            if op['kind'] == 'OAUTH':
                require(set(op) == {'kind', 'generation', 'reconnect', 'retest_package', 'sequence'}
                        and type(op['reconnect']) is bool, 'RECOVERY_REQUIRED')
                inferred = totals['oauth'] > 0
                require(op['reconnect'] is inferred, 'RECOVERY_REQUIRED')
                require(totals['oauth'] < self.limits.oauth, 'RECOVERY_REQUIRED')
                require(not inferred or totals['reconnect'] < 3, 'RECOVERY_REQUIRED')
                self._retest(totals, op['retest_package'], oauth=1)
                totals['oauth'] += 1
                totals['reconnect'] += int(inferred)
            elif op['kind'] == 'REQUEST':
                required = {'kind', 'generation', 'lane', 'bucket', 'file', 'rollback', 'create', 'refresh', 'retest_package', 'cleanup', 'intent_ref', 'sequence'}
                require(set(op) == required and type(op['lane']) is str and op['lane'] in {'normal', 'safety'}
                        and (op['file'] is None or (type(op['file']) is str and op['file'] in {'A', 'B', 'C'}))
                        and type(op['bucket']) is str
                        and all(type(op[name]) is bool for name in ('rollback', 'create', 'refresh')),
                        'RECOVERY_REQUIRED')
                require(op['intent_ref'] is None or (type(op['intent_ref']) is str and op['intent_ref'] in pins
                        and pins[op['intent_ref']]['generation'] == op['generation']), 'RECOVERY_REQUIRED')
                caps = self.limits.buckets(op['lane'])
                require(op['bucket'] in caps and (not op['create'] or (op['file'] == 'C' and not op['rollback'])),
                        'RECOVERY_REQUIRED')
                require(type(op['cleanup']) is bool
                        and (not op['cleanup'] or (op['lane'] == 'safety' and op['bucket'] == 'revoke'
                                                   and op['file'] is None and not op['rollback']
                                                   and not op['create'] and not op['refresh'])),
                        'RECOVERY_REQUIRED')
                require(totals['api_total'] < self.limits.api_total
                        and totals['api_' + op['lane']] < getattr(self.limits, 'api_' + op['lane'])
                        and totals['buckets'][op['bucket']] < caps[op['bucket']]
                        and (not op['refresh'] or totals['refresh'] < self.limits.refresh), 'RECOVERY_REQUIRED')
                file = op['file']
                if file:
                    require(totals['writes'][file] < getattr(self.limits, 'writes_' + file.lower())
                            and (not op['rollback'] or totals['rollbacks'][file] < getattr(self.limits, 'rollbacks_' + file.lower()))
                            and (op['rollback'] or totals['nonrollbacks'][file] < getattr(self.limits, 'nonrollbacks_' + file.lower()))
                            and (not op['create'] or totals['creates_c'] < self.limits.creates_c),
                            'RECOVERY_REQUIRED')
                self._retest(totals, op['retest_package'], api=1,
                             rollback=int(op['rollback'] and bool(file)),
                             nonrollback=int((not op['rollback']) and bool(file)))
                totals['api_total'] += 1
                totals['api_' + op['lane']] += 1
                totals['buckets'][op['bucket']] += 1
                totals['refresh'] += int(op['refresh'])
                if file:
                    totals['writes'][file] += 1
                    totals['rollbacks'][file] += int(op['rollback'])
                    totals['nonrollbacks'][file] += int(not op['rollback'])
                    totals['creates_c'] += int(op['create'])
            elif op['kind'] == 'WRITE_INTENT':
                require(set(op) == {'kind', 'generation', 'resource', 'revision', 'intent_hash', 'recovery_hash', 'sequence'}
                        and type(op['resource']) is str and type(op['revision']) is str and bool(op['revision'])
                        and all(type(op[k]) is str and len(op[k]) == 64 and all(c in '0123456789abcdef' for c in op[k])
                                for k in ('intent_hash', 'recovery_hash')), 'RECOVERY_REQUIRED')
                pins[key] = op
            else:
                raise Denied('RECOVERY_REQUIRED')
        self._validate_totals(totals)
        return totals

    def _validate_totals(self, totals):
        required = {'oauth', 'reconnect', 'refresh', 'api_total', 'api_normal', 'api_safety',
                    'writes', 'rollbacks', 'nonrollbacks', 'creates_c', 'buckets', 'retest'}
        require(set(totals) == required and all(type(totals[k]) is int and totals[k] >= 0
                for k in ('oauth', 'reconnect', 'refresh', 'api_total', 'api_normal', 'api_safety', 'creates_c')),
                'RECOVERY_REQUIRED')
        require(all(type(totals[k]) is dict for k in ('writes', 'rollbacks', 'nonrollbacks', 'buckets', 'retest')),
                'RECOVERY_REQUIRED')
        require(totals['oauth'] <= self.limits.oauth and totals['reconnect'] <= 3
                and totals['reconnect'] <= totals['oauth'] and totals['refresh'] <= self.limits.refresh
                and totals['api_total'] == totals['api_normal'] + totals['api_safety'] <= self.limits.api_total,
                'RECOVERY_REQUIRED')
        expected_buckets = set(dict(self.limits.ordinary_buckets + self.limits.safety_buckets))
        require(set(totals['buckets']) == expected_buckets
                and all(type(value) is int and value >= 0 for value in totals['buckets'].values()),
                'RECOVERY_REQUIRED')
        for lane in ('normal', 'safety'):
            caps = self.limits.buckets(lane)
            require(totals['api_' + lane] == sum(totals['buckets'][name] for name in caps)
                    and totals['api_' + lane] <= getattr(self.limits, 'api_' + lane)
                    and all(totals['buckets'][name] <= cap for name, cap in caps.items()), 'RECOVERY_REQUIRED')
        for file in ('A', 'B', 'C'):
            require(set(totals['writes']) == {'A', 'B', 'C'} and set(totals['rollbacks']) == {'A', 'B', 'C'}
                    and set(totals['nonrollbacks']) == {'A', 'B', 'C'}
                    and all(type(table[file]) is int and table[file] >= 0
                            for table in (totals['writes'], totals['rollbacks'], totals['nonrollbacks']))
                    and totals['writes'][file] == totals['rollbacks'][file] + totals['nonrollbacks'][file]
                    and totals['writes'][file] <= getattr(self.limits, 'writes_' + file.lower())
                    and totals['rollbacks'][file] <= getattr(self.limits, 'rollbacks_' + file.lower())
                    and totals['nonrollbacks'][file] <= getattr(self.limits, 'nonrollbacks_' + file.lower()),
                    'RECOVERY_REQUIRED')
        require(totals['creates_c'] <= min(self.limits.creates_c, totals['nonrollbacks']['C'])
                and set(totals['retest']) == {'1', '2'}, 'RECOVERY_REQUIRED')
        for record in totals['retest'].values():
            require(type(record) is dict and set(record) == {'api', 'oauth', 'rollback', 'nonrollback'}
                    and all(type(value) is int and value >= 0 for value in record.values())
                    and record['api'] <= 20 and record['oauth'] <= 1
                    and record['rollback'] <= 1 and record['nonrollback'] <= 2,
                    'RECOVERY_REQUIRED')

    def _save(self, data):
        self.store.write(data)
        require(self.store.read() == data, 'LEDGER_READBACK_FAILED')
