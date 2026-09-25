"""Synthetic-only integration; HTTPS and encrypted-file I/O are explicit doubles.

Native DPAPI and native ledger qualification live in their separate test suites.
No real account, credential target, document, or provider request is used here.
"""
from dataclasses import asdict, replace
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from companion_mind.connector_s3.docs_plan import canonical, digest, parse_document, replace_unique
from companion_mind.connector_s3.executor import WriteExecutor
from companion_mind.connector_s3.native import NativeLedger, S3CredentialStore, PREFIX, current_sid
from companion_mind.connector_s3.policy import Denied, MemoryState
from companion_mind.connector_s3.provider import GoogleProvider
from companion_mind.connector_s3.recovery import RecoveryReceipt, RecoveryStore, UserDPAPI
from companion_mind.connector_s3.stage import StageLifecycle
from tests.test_connector_s3_docs_plan import document
from tests.test_connector_s3_provider import FakeHTTPS, Response, TOKEN, binding, metadata


class Secrets:
    def __init__(self): self.reads = 0
    def business_token(self, ref):
        assert ref == 'synthetic-token-ref'
        self.reads += 1
        return TOKEN


class Transactions(unittest.TestCase):
    def setUp(self):
        self.binding = binding()
        self.store = MemoryState()
        self.stage = StageLifecycle(self.binding, self.store)
        self.stage.initialize()
        self.packages = {}
        dpapi = object.__new__(UserDPAPI)
        dpapi.sid = self.binding.sid
        self.recovery = RecoveryStore('.', digest(canonical(asdict(self.binding))), dpapi)
        self.secrets = Secrets()
        self.saved_at_connections = []

        def save(store, key, raw):
            self.assertNotIn(key, self.packages)
            self.packages[key] = raw
            self.saved_at_connections.append(len(FakeHTTPS.instances))
            return RecoveryReceipt(key, store.binding_hash, digest(b'synthetic-cipher' + raw), digest(raw), len(raw))

        def load(store, receipt):
            require_raw = self.packages[receipt.operation_id]
            if digest(require_raw) != receipt.plain_hash:
                raise Denied('RECOVERY_PACKAGE_INVALID')
            return require_raw

        self.save_patch = patch.object(RecoveryStore, 'save', save)
        self.load_patch = patch.object(RecoveryStore, 'load', load)
        self.http_patch = patch('companion_mind.connector_s3.provider.http.client.HTTPSConnection', FakeHTTPS)
        for item in (self.save_patch, self.load_patch, self.http_patch):
            item.start(); self.addCleanup(item.stop)
        FakeHTTPS.instances, FakeHTTPS.responses, FakeHTTPS.failure = [], [], False
        FakeHTTPS.closes_after_headers = False
        self.before = parse_document(document('before 😀\n'), self.binding.a_id)
        self.plan = replace_unique(self.before, 'before', 'after')
        self.executor = self.reopen()

    def reopen(self):
        stage = StageLifecycle(self.binding, self.store)
        return WriteExecutor(stage, self.binding, self.recovery, GoogleProvider(self.binding),
                             self.secrets, 'synthetic-token-ref', clock=lambda: 1)

    def responses(self, before, after):
        return [Response(metadata(self.binding.a_id)), Response(document(before, 'revision-1')),
                Response(), Response(metadata(self.binding.a_id)), Response(document(after, 'revision-2'))]

    def write(self):
        FakeHTTPS.responses = self.responses('before 😀\n', 'after 😀\n')
        return self.executor.write('edit', self.plan)

    def posts(self):
        return [call for c in FakeHTTPS.instances for call in c.calls if call[0] == 'POST']

    def test_write_restore_full_chain_pins_receipts_and_consumes_same_stage(self):
        self.assertEqual(self.write()['status'], 'VERIFIED')
        tx = self.stage.snapshot()['transactions']['edit']
        self.assertEqual((tx['held_api'], tx['held_write']), (5, 1))
        self.assertEqual(self.saved_at_connections, [2])
        FakeHTTPS.responses = self.responses('after 😀\n', 'before 😀\n')
        self.assertEqual(self.reopen().restore('edit')['status'], 'RESTORED')
        data = self.stage.snapshot()
        self.assertEqual((data['totals']['api_normal'], data['totals']['api_safety']), (5, 5))
        self.assertEqual((data['totals']['writes']['A'], data['totals']['rollbacks']['A']), (2, 1))
        self.assertEqual(data['transactions']['edit']['held_api'], 0)
        self.assertEqual(self.saved_at_connections, [2, 7])
        self.assertEqual(len(self.posts()), 2)
        self.assertNotIn('before 😀', str(data))

    def test_replay_and_other_broker_writes_cannot_bypass_open_file(self):
        self.write()
        count = len(FakeHTTPS.instances)
        with self.assertRaisesRegex(Denied, 'OPERATION_REPLAY'):
            self.reopen().write('edit', self.plan)
        with self.assertRaisesRegex(Denied, 'FILE_RECOVERY_REQUIRED'):
            self.stage.reserve_request(self.binding, 'bypass', now=1, lane='normal', bucket='A_B_flow', file='A')
        self.assertEqual(len(FakeHTTPS.instances), count)

    def test_changed_source_or_parent_never_sends_write(self):
        FakeHTTPS.responses = [Response(metadata(self.binding.a_id)), Response(document('external\n'))]
        with self.assertRaisesRegex(Denied, 'SOURCE_CHANGED'):
            self.executor.write('changed', self.plan)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.packages, {})
        bad = metadata(self.binding.a_id); bad['parents'] = ['unselected-folder']
        FakeHTTPS.responses = [Response(bad)]
        with self.assertRaisesRegex(Denied, 'RESOURCE_PARENT_DENIED'):
            self.executor.write('parent', self.plan)
        self.assertEqual(self.posts(), [])

    def test_recovery_failure_prevents_write_and_never_pins_a_fake_receipt(self):
        FakeHTTPS.responses = self.responses('before 😀\n', 'after 😀\n')
        with patch.object(RecoveryStore, 'load', side_effect=Denied('RECOVERY_PACKAGE_UNAVAILABLE')):
            with self.assertRaisesRegex(Denied, 'RECOVERY_PACKAGE_UNAVAILABLE'):
                self.executor.write('missing', self.plan)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.stage.snapshot()['transactions'], {})
        self.assertEqual(self.stage.snapshot()['totals']['api_total'], 2)

    def test_no_restoration_capacity_rejects_before_write(self):
        for i in range(36):
            self.stage.reserve_request(self.binding, 'spent-%d' % i, now=1, lane='safety',
                                       bucket='restore_existing_and_readback')
        FakeHTTPS.responses = self.responses('before 😀\n', 'after 😀\n')
        with self.assertRaisesRegex(Denied, 'RESTORE_RESERVE_EXHAUSTED'):
            self.executor.write('capacity', self.plan)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.stage.snapshot()['totals']['writes']['A'], 0)

    def test_other_requests_cannot_consume_held_capacity(self):
        self.write()
        for i in range(35):
            self.stage.reserve_request(self.binding, 'safe-%d' % i, now=1, lane='safety',
                                       bucket='restore_existing_and_readback')
        with self.assertRaisesRegex(Denied, 'API_BUCKET_EXHAUSTED'):
            self.stage.reserve_request(self.binding, 'steal', now=1, lane='safety',
                                       bucket='restore_existing_and_readback')
        FakeHTTPS.responses = self.responses('after 😀\n', 'before 😀\n')
        self.assertEqual(self.reopen().restore('edit')['status'], 'RESTORED')
        self.assertEqual(self.stage.snapshot()['totals']['buckets']['restore_existing_and_readback'], 40)

    def test_external_edit_prevents_rollback_and_stays_quarantined(self):
        self.write()
        FakeHTTPS.responses = [Response(metadata(self.binding.a_id)), Response(document('external edit\n'))]
        with self.assertRaisesRegex(Denied, 'RESTORE_OUTCOME_UNKNOWN'):
            self.reopen().restore('edit')
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.stage.snapshot()['transactions']['edit']['phase'], 'UNKNOWN')
        with self.assertRaisesRegex(Denied, 'RESTORE_DENIED'):
            self.reopen().restore('edit')

    def test_write_error_is_charged_quarantined_and_exact_reconciliation_never_retries(self):
        FakeHTTPS.responses = [Response(metadata(self.binding.a_id)), Response(document('before 😀\n')),
                               Response(status=503)]
        with self.assertRaisesRegex(Denied, 'WRITE_OUTCOME_UNKNOWN'):
            self.executor.write('edit', self.plan)
        self.assertEqual(self.stage.snapshot()['totals']['api_total'], 5)
        self.assertEqual(len(self.posts()), 1)
        FakeHTTPS.responses = [Response(metadata(self.binding.a_id)), Response(document('after 😀\n'))]
        self.assertEqual(self.reopen().reconcile('edit', 'probe1')['status'], 'VERIFIED')
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.stage.snapshot()['totals']['buckets']['exact_unknown_reconciliation'], 2)

    def test_crash_before_or_after_dispatch_has_no_replay_ticket_on_restart(self):
        FakeHTTPS.responses = self.responses('before 😀\n', 'after 😀\n')
        with patch.object(GoogleProvider, 'write_document', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.executor.write('edit', self.plan)
        self.assertEqual(self.stage.snapshot()['transactions']['edit']['phase'], 'WRITING')
        with self.assertRaisesRegex(Denied, 'OPERATION_REPLAY'):
            self.reopen().write('edit', self.plan)
        FakeHTTPS.responses = [Response(metadata(self.binding.a_id)), Response(document('before 😀\n'))]
        self.assertEqual(self.reopen().reconcile('edit', 'probe1')['status'], 'RESTORED')
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.stage.snapshot()['totals']['writes']['A'], 1)

    def test_revoke_and_closed_generation_refuse_before_secret_access(self):
        self.write()
        count = self.secrets.reads
        self.stage.revoke_local(self.binding)
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'):
            self.reopen().restore('edit')
        self.assertEqual(self.secrets.reads, count)

    def test_receipt_substitution_and_budget_corruption_fail_on_reopen(self):
        self.write()
        saved = copy.deepcopy(self.store.raw)
        self.store.raw['transactions']['edit']['receipt']['plain_hash'] = 'f' * 64
        with self.assertRaisesRegex(Denied, 'TRANSACTION_RECORD_INVALID'):
            self.stage.snapshot()
        self.store.raw = saved
        self.store.raw['transactions']['edit']['held_api'] = 0
        with self.assertRaisesRegex(Denied, 'TRANSACTION_RECORD_INVALID'):
            self.stage.snapshot()

    def test_retest_rollback_reservation_shares_package_cap(self):
        self.stage.reserve_request(self.binding, 'used-rollback', now=1, lane='safety',
                                   bucket='restore_existing_and_readback', file='B', rollback=True, retest_package=1)
        FakeHTTPS.responses = self.responses('before 😀\n', 'after 😀\n')
        with self.assertRaisesRegex(Denied, 'RESTORE_RESERVE_EXHAUSTED'):
            self.executor.write('package', self.plan, retest_package=1)
        self.assertEqual(self.posts(), [])

    def test_failed_atomic_prepare_does_not_dispatch_or_leave_uncounted_write(self):
        FakeHTTPS.responses = self.responses('before 😀\n', 'after 😀\n')
        original = self.store.write
        def fail_prepare(data):
            if data['transactions']:
                raise OSError('synthetic storage failure')
            original(data)
        with patch.object(self.store, 'write', fail_prepare):
            with self.assertRaises(OSError):
                self.executor.write('edit', self.plan)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.stage.snapshot()['totals']['api_total'], 2)
        self.assertEqual(self.stage.snapshot()['totals']['writes']['A'], 0)


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI/native ledger integration only')
class NativeTransaction(unittest.TestCase):
    def test_dpapi_native_witness_reopen_restore_and_cleanup_with_fake_https(self):
        bound = replace(binding(), sid=current_sid())
        native = S3CredentialStore(bound.sid)
        target = PREFIX + 'Synthetic.' + str(uuid4())
        self.assertIsNone(native.read_record(target, persist=2))
        FakeHTTPS.instances, FakeHTTPS.responses, FakeHTTPS.failure = [], [], False
        FakeHTTPS.closes_after_headers = False
        try:
            with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                ledger = NativeLedger(Path(directory) / 'ledger', native, target, 'synthetic-stage')
                stage = StageLifecycle(bound, ledger)
                stage.initialize()
                recovery = RecoveryStore(Path(directory) / 'recovery', digest(canonical(asdict(bound))), UserDPAPI(bound.sid))
                secrets = Secrets()
                before = parse_document(document('before 😀\n'), bound.a_id)
                plan = replace_unique(before, 'before', 'after')
                with patch('companion_mind.connector_s3.provider.http.client.HTTPSConnection', FakeHTTPS):
                    FakeHTTPS.responses = [Response(metadata(bound.a_id)), Response(document('before 😀\n')),
                                           Response(), Response(metadata(bound.a_id)), Response(document('after 😀\n'))]
                    WriteExecutor(stage, bound, recovery, GoogleProvider(bound), secrets,
                                  'synthetic-token-ref', clock=lambda: 1).write('edit', plan)
                    self.assertEqual(stage.snapshot()['transactions']['edit']['held_api'], 5)
                    cipher = next((Path(directory) / 'recovery').iterdir()).read_bytes()
                    self.assertNotIn('before 😀'.encode(), cipher)
                    reopened = StageLifecycle(bound, NativeLedger(Path(directory) / 'ledger', native, target, 'synthetic-stage'))
                    recovery = RecoveryStore(Path(directory) / 'recovery', digest(canonical(asdict(bound))), UserDPAPI(bound.sid))
                    FakeHTTPS.responses = [Response(metadata(bound.a_id)), Response(document('after 😀\n')),
                                           Response(), Response(metadata(bound.a_id)), Response(document('before 😀\n'))]
                    result = WriteExecutor(reopened, bound, recovery, GoogleProvider(bound), secrets,
                                           'synthetic-token-ref', clock=lambda: 1).restore('edit')
                    self.assertEqual(result['status'], 'RESTORED')
                    self.assertEqual(reopened.snapshot()['totals']['api_total'], 10)
                    self.assertEqual(len(FakeHTTPS.instances), 10)
                    reopened.revoke_local(bound)
                    count = secrets.reads
                    with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'):
                        WriteExecutor(reopened, bound, recovery, GoogleProvider(bound), secrets,
                                      'synthetic-token-ref', clock=lambda: 1).restore('edit')
                    self.assertEqual(secrets.reads, count)
        finally:
            native.delete(target)
            self.assertIsNone(native.read_record(target, persist=2))


if __name__ == '__main__':
    unittest.main()
