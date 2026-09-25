import copy
from contextlib import contextmanager
import multiprocessing as mp
import os
from pathlib import Path
from threading import RLock
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from companion_mind.connector_s3.native import NativeLedger, S3CredentialStore, PREFIX, current_sid
from companion_mind.connector_s3.policy import Binding, Controller, Denied
from companion_mind.connector_s3.stage import GenerationBinding, StageLifecycle


class FakeStore:
    def __init__(self):
        self.records = {}
        self.lock = RLock()
    @contextmanager
    def mutex(self, target):
        with self.lock:
            yield
    def read_record(self, target, persist=2):
        assert persist == 2
        return copy.deepcopy(self.records.get(target))
    def write_record(self, target, value, persist=2):
        assert persist == 2
        self.records[target] = copy.deepcopy(value)


def binding():
    return Binding(1, 'folder', 'file-a', 'file-b', 'client', 'project', 'synthetic-sid')


def stage_binding(generation=1):
    return GenerationBinding(generation, 'account', 'subject', 'googleapis.com', 'client', 'project',
                             'drive.file', 'synthetic-sid', 0, 500, 'folder', 'file-a', 'file-b')


def _compete(directory, target, ready, output):
    try:
        store = S3CredentialStore(current_sid())
        ledger = NativeLedger(directory, store, target, 'synthetic-stage')
        controller = Controller(binding(), ledger)
        ready.wait(10)
        accepted = 0
        for i in range(20):
            try:
                controller.reserve('%s-%s' % (os.getpid(), i), lane='normal', bucket='initialization')
                accepted += 1
            except Denied as exc:
                if str(exc) != 'API_BUCKET_EXHAUSTED':
                    raise
        output.put(('ok', accepted))
    except Exception as exc:
        output.put(('error', type(exc).__name__))


class NativeLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.store = FakeStore()
        self.target = PREFIX + 'Synthetic.' + str(uuid4())
        self.ledger = NativeLedger(self.temp.name, self.store, self.target, 'synthetic-stage')
    def tearDown(self):
        self.temp.cleanup()
    def test_explicit_initialization_and_reopen(self):
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            self.ledger.read()
        self.ledger.initialize({'count': 0})
        other = NativeLedger(self.temp.name, self.store, self.target, 'synthetic-stage')
        other.write({'count': 1})
        self.assertEqual(self.ledger.read(), {'count': 1})
        self.assertEqual(self.store.records[self.target]['sequence'], 2)
        with self.assertRaisesRegex(Denied, 'LEDGER_ALREADY_EXISTS'):
            self.ledger.initialize({'count': 0})
    def test_disk_snapshot_rollback_is_rejected_by_native_witness(self):
        self.ledger.initialize({'count': 0})
        old = self.ledger.state.read_bytes()
        self.ledger.write({'count': 1})
        self.ledger.state.write_bytes(old)
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            self.ledger.read()
    def test_missing_state_and_missing_witness_do_not_reset(self):
        self.ledger.initialize({'count': 1})
        saved = self.ledger.state.read_bytes()
        self.ledger.state.unlink()
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            self.ledger.read()
        with self.assertRaisesRegex(Denied, 'LEDGER_ALREADY_EXISTS'):
            self.ledger.initialize({'count': 0})
        self.ledger.state.write_bytes(saved)
        self.store.records.clear()
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            self.ledger.read()
        with self.assertRaisesRegex(Denied, 'LEDGER_ALREADY_EXISTS'):
            self.ledger.initialize({'count': 0})
    def test_crash_after_native_prepare_is_fail_closed(self):
        self.ledger.initialize({'count': 0})
        with patch('companion_mind.connector_s3.native.os.replace', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.ledger.write({'count': 1})
        self.assertEqual(self.store.records[self.target]['phase'], 'PREPARED')
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            self.ledger.read()
    def test_crash_after_file_replace_before_native_commit_is_fail_closed(self):
        self.ledger.initialize({'count': 0})
        original = self.store.write_record
        def fail_commit(target, record, persist=2):
            if record['phase'] == 'COMMITTED':
                raise OSError('synthetic')
            original(target, record, persist)
        with patch.object(self.store, 'write_record', side_effect=fail_commit):
            with self.assertRaises(OSError):
                self.ledger.write({'count': 1})
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            self.ledger.read()
    def test_wrong_stage_or_path_rejects_witness_reuse(self):
        self.ledger.initialize({'count': 1})
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            NativeLedger(self.temp.name, self.store, self.target, 'wrong-stage').read()
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            NativeLedger(Path(self.temp.name) / 'other', self.store, self.target, 'synthetic-stage').read()
    def test_policy_reservation_is_one_counted_commit_and_restart_cannot_dispatch(self):
        c = Controller(binding(), self.ledger)
        c.initialize()
        sequence = self.store.records[self.target]['sequence']
        epoch = c.reserve('pending', lane='normal', bucket='A_B_flow', file='A')
        data = self.ledger.read()
        self.assertEqual(self.store.records[self.target]['sequence'], sequence + 1)
        self.assertEqual((data['api_total'], data['writes']['A']), (1, 1))
        reopened = Controller(binding(), self.ledger)
        with self.assertRaisesRegex(Denied, 'DISPATCH_DENIED'):
            reopened.dispatch('pending', epoch)
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            reopened.recover()
        c.dispatch('pending', epoch)
        c.complete('pending', 'VERIFIED')
    def test_failed_charge_never_mints_a_dispatch_ticket(self):
        c = Controller(binding(), self.ledger)
        c.initialize()
        with patch.object(self.ledger, 'write', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                c.reserve('no-ticket', lane='normal', bucket='initialization')
        with self.assertRaisesRegex(Denied, 'DISPATCH_DENIED'):
            c.dispatch('no-ticket', 0)
    def test_stage_budget_survives_native_ledger_reopen(self):
        one = stage_binding()
        lifecycle = StageLifecycle(one, self.ledger)
        lifecycle.initialize()
        lifecycle.start_oauth(one, 'oauth', now=1)
        lifecycle.reserve_request(one, 'write-a', now=1, lane='normal', bucket='A_B_flow', file='A')
        lifecycle.close_generation(1)
        two = stage_binding(2)
        lifecycle.start_next_generation(two)
        reopened_ledger = NativeLedger(self.temp.name, self.store, self.target, 'synthetic-stage')
        reopened = StageLifecycle(two, reopened_ledger)
        reopened.reserve_request(two, 'refresh', now=1, lane='normal', bucket='continuity', refresh=True)
        totals = reopened.snapshot()['totals']
        self.assertEqual((totals['oauth'], totals['refresh'], totals['writes']['A']), (1, 1, 1))


@unittest.skipUnless(os.name == 'nt', 'Windows native qualification only')
class WindowsNativeCanary(unittest.TestCase):
    def test_uuid_native_tokens_mutex_and_two_process_budget(self):
        store = S3CredentialStore(current_sid())
        target = PREFIX + 'Synthetic.' + str(uuid4())
        token_target = target + '.Tokens'
        targets = [target, token_target]
        self.assertTrue(all(store.read_record(t, persist=2) is None for t in targets))
        processes = []
        try:
            # Only newly minted canary values and UUID slots are ever touched.
            store.write_tokens(token_target, 'synthetic-access-' + str(uuid4()), 'synthetic-refresh-' + str(uuid4()))
            self.assertEqual(set(store.read_tokens(token_target)), {'access_token', 'refresh_token'})
            with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                ledger = NativeLedger(directory, store, target, 'synthetic-stage')
                Controller(binding(), ledger).initialize()
                context = mp.get_context('spawn')
                ready, output = context.Event(), context.Queue()
                processes = [context.Process(target=_compete, args=(directory, target, ready, output)) for _ in range(2)]
                for process in processes:
                    process.start()
                ready.set()
                results = [output.get(timeout=45) for _ in processes]
                for process in processes:
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
                self.assertTrue(all(result[0] == 'ok' for result in results), results)
                self.assertEqual(sum(result[1] for result in results), 30)
                self.assertEqual(ledger.read()['api_total'], 30)
                self.assertEqual(ledger.read()['buckets']['initialization'], 30)
                Controller(binding(), ledger).revoke_local()
                with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'):
                    Controller(binding(), ledger).reserve('after-revoke', lane='normal', bucket='continuity')
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            for slot in targets:
                store.delete(slot)
            self.assertTrue(all(store.read_record(slot, persist=2) is None for slot in targets))


if __name__ == '__main__':
    unittest.main()

