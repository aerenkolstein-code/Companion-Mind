"""Only the new synthetic library; no legacy suite or dispatcher imports."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import threading
import unittest
from unittest.mock import patch
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT.parents[1] / '07_P3S3' / 'WO-A1-WEBCHAT-Q1LOCAL-20261004-01'
RUN = EVIDENCE / ('tests-run' + os.environ.get('Q1_TEST_RUN', '01'))
spec = importlib.util.spec_from_file_location('q1_store_only', ROOT / 'companion_mind/webchat_q1/store.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Store = module.LocalWorkOrderStore
WO = json.loads((ROOT / 'companion_mind/webchat_q1/synthetic_work_order.json').read_text(encoding='utf-8'))
PAYLOAD = json.dumps({'files': {'synthetic/input.txt': 'synthetic candidate only'}})
COUNTS = {}
LOCK = threading.Lock()


def call(store, method, *args):
    with LOCK:
        COUNTS[method] = COUNTS.get(method, 0) + 1
    return getattr(store, method)(*args)


def make(path):
    with LOCK:
        COUNTS['constructor'] = COUNTS.get('constructor', 0) + 1
    return Store(path, WO)


class Q1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RUN.mkdir(exist_ok=False)
        cls.guards = [patch('socket.socket', side_effect=AssertionError('network forbidden')),
                      patch('subprocess.Popen', side_effect=AssertionError('process forbidden')),
                      patch('webbrowser.open', side_effect=AssertionError('browser forbidden')),
                      patch.object(ctypes, 'WinDLL', side_effect=AssertionError('native forbidden'), create=True)]
        cls.mocks = [guard.start() for guard in cls.guards]

    @classmethod
    def tearDownClass(cls):
        sentinel_counts = [mock.call_count for mock in cls.mocks]
        for guard in cls.guards: guard.stop()
        print('Q1_COUNTS=' + json.dumps({'api_and_constructor': COUNTS,
              'external_sentinel_calls': sentinel_counts, 'dispatcher_interface_calls': 0}, sort_keys=True))
        if any(sentinel_counts): raise AssertionError('external boundary touched')

    def setUp(self):
        self.path = RUN / (self._testMethodName + '.db')
        self.store = make(self.path)

    def status(self, store=None):
        return call(store or self.store, 'get_status', WO['wo_id'], 'worker')

    def submit(self, cid='c1', payload=PAYLOAD, store=None, **changes):
        args = dict(wo_id=WO['wo_id'], actor='worker', version=1, candidate_id=cid,
                    base_sha256=WO['base_hash'], candidate_text=payload)
        args.update(changes)
        return call(store or self.store, 'submit_candidate', *args.values())

    def attest(self, owner, cid='c1', verdict='PASS'):
        return call(self.store, 'attest', WO['wo_id'], owner, cid, verdict)

    def finalize(self, cid='c1'):
        return call(self.store, 'finalize', WO['wo_id'], cid)

    def test_identity_version_base(self):
        self.assertTrue(call(self.store, 'fetch_work_order', WO['wo_id'], 'worker', 1)['ok'])
        for version in [2, None, True]:
            self.assertEqual(call(self.store, 'fetch_work_order', WO['wo_id'], 'worker', version)['error'], 'VERSION_MISMATCH')
            self.assertEqual(self.submit(version=version)['error'], 'VERSION_MISMATCH')
        for change, code in [({'actor': 'owner'}, 'WRONG_IDENTITY'), ({'wo_id': 'wrong'}, 'WORK_ORDER_NOT_FOUND'),
                             ({'base_sha256': '0'*64}, 'BASE_HASH_MISMATCH')]:
            result = self.submit(**change)
            self.assertEqual(result['error'], code)
            self.assertEqual(result['error_kind'], 'CONTRACT')
        self.assertEqual(self.status()['candidate_count'], 0)

    def test_candidate_path_and_shape(self):
        bad = [{'files': {'../input.txt': 'x'}}, {'files': {'C:/input.txt': 'x'}},
               {'files': {'/input.txt': 'x'}}, {'files': {'synthetic/input.txt': 'x', 'other': 'x'}},
               {'files': {'synthetic/input.txt': 'x'}, 'patch': 'x'},
               {'files': {'synthetic/input.txt': 'x'*16385}}]
        for item in bad:
            self.assertEqual(self.submit(payload=json.dumps(item))['error'], 'INVALID_CANDIDATE')
        self.assertEqual(self.submit(payload='{"files":{},"files":{}}')['error'], 'INVALID_CANDIDATE')
        for cid in ['../x', '', 'x'*65, 'a/b']:
            self.assertEqual(self.submit(cid=cid)['error'], 'INVALID_CANDIDATE_ID')
        self.assertEqual(self.status()['event_count'], 1)

    def test_duplicate_conflict_and_readonly_status(self):
        first = self.submit()
        before = self.status()
        duplicate = self.submit(payload=json.dumps(json.loads(PAYLOAD), separators=(',', ':')))
        self.assertFalse(first['duplicate']); self.assertTrue(duplicate['duplicate'])
        self.assertEqual(first['candidate_hash'], duplicate['candidate_hash'])
        self.assertEqual(self.submit(payload=json.dumps({'files': {'synthetic/input.txt': 'different'}}))['error'], 'CANDIDATE_CONFLICT')
        self.assertEqual(before, self.status()); self.assertEqual(before, self.status())

    def test_unknown_persists_and_stops(self):
        self.submit()
        call(self.store, 'mark_unknown', WO['wo_id'])
        other = make(self.path)
        self.assertEqual(self.status(other)['state'], 'UNKNOWN')
        self.assertFalse(self.submit(cid='c2', store=other)['ok'])
        self.assertFalse(self.finalize()['ok'])
        before = self.status()
        self.assertTrue(self.submit()['duplicate'])
        self.assertEqual(before, self.status())

    def test_cancel_preserves_history(self):
        self.submit()
        call(self.store, 'cancel', WO['wo_id'])
        other = make(self.path)
        self.assertEqual(self.status(other)['state'], 'STOP')
        self.assertEqual(self.status(other)['candidate_count'], 1)
        self.assertFalse(self.submit(cid='c2', store=other)['ok'])
        self.assertTrue(call(other, 'cancel', WO['wo_id'])['duplicate'])

    def test_owner_separation_and_unique_terminal(self):
        self.assertEqual(self.submit()['status'], 'CANDIDATE_PENDING')
        self.assertFalse(self.finalize()['ok'])
        self.assertEqual(self.attest('worker')['error'], 'OWNER_DENIED')
        self.attest('A1_OWNER')
        self.assertFalse(self.finalize()['ok'])
        self.attest('CMER_OWNER')
        self.assertEqual(self.status()['state'], 'OPEN')
        self.assertEqual(self.finalize()['status'], 'ACCEPTED')
        before = self.status()
        self.assertTrue(self.finalize()['duplicate'])
        self.assertEqual(before, self.status())
        self.assertFalse(self.finalize('different')['ok'])
        self.assertEqual(self.status(make(self.path))['state'], 'ACCEPTED')

    def test_owners_must_sign_same_candidate(self):
        self.submit(); self.submit('c2')
        self.attest('A1_OWNER', 'c1'); self.attest('CMER_OWNER', 'c2')
        self.assertFalse(self.finalize('c1')['ok']); self.assertFalse(self.finalize('c2')['ok'])
        self.assertIsNone(self.status()['terminal'])

    def test_not_pass_is_terminal_stop(self):
        self.submit(); self.attest('A1_OWNER', verdict='NOT_PASS')
        self.assertEqual(self.status()['state'], 'STOP')
        self.assertFalse(self.attest('CMER_OWNER')['ok'])
        self.assertFalse(self.finalize()['ok'])
        self.assertFalse(self.submit('c2')['ok'])

    def test_independent_database_copy(self):
        self.submit()
        copied = RUN / 'independent-copy.db'
        shutil.copyfile(self.path, copied)
        second = make(copied)
        self.assertTrue(self.submit(store=second)['duplicate'])
        self.submit('c2', store=second)
        self.assertEqual(self.status()['candidate_count'], 1)
        self.assertEqual(self.status(second)['candidate_count'], 2)

    def test_simultaneous_same_nonce(self):
        stores = [make(self.path) for _ in range(4)]
        barrier = threading.Barrier(4)
        results = []
        def worker(store):
            barrier.wait()
            results.append(self.submit(store=store))
        threads = [threading.Thread(target=worker, args=(store,)) for store in stores]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result['ok'] for result in results))
        self.assertEqual(sum(not result['duplicate'] for result in results), 1)
        self.assertEqual(self.status()['candidate_count'], 1)
        self.assertEqual(self.status()['event_count'], 2)

    def test_corruption_and_configuration_drift(self):
        damaged = RUN / 'damaged.db'
        damaged.write_bytes(b'not a sqlite database')
        with self.assertRaisesRegex(module.StoreError, '^STORAGE_UNTRUSTED$'): make(damaged)
        self.assertEqual(damaged.read_bytes(), b'not a sqlite database')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE metadata SET work_order='{}'")
        self.assertEqual(self.status()['error_kind'], 'UNCERTAIN')
        with self.assertRaisesRegex(module.StoreError, '^WORK_ORDER_CONFIGURATION_MISMATCH$'): make(self.path)

    def test_event_tamper_and_uncertain_io(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE events SET hash='bad'")
        self.assertEqual(self.status()['error'], 'STORAGE_UNTRUSTED')
        clean = make(RUN / 'uncertain-io.db')
        with patch.object(clean, '_connect', side_effect=sqlite3.OperationalError('private details')):
            result = self.status(clean)
        self.assertEqual(result, {'ok': False, 'error': 'IO_COMMIT_UNKNOWN', 'error_kind': 'UNCERTAIN'})

    def test_persisted_candidate_damage_is_not_contract_rejection(self):
        self.submit()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE candidates SET payload='invalid-json'")
        self.assertEqual(self.status(), {'ok': False, 'error': 'STORAGE_UNTRUSTED', 'error_kind': 'UNCERTAIN'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
