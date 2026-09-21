"""No-network lifecycle and failure tests; native probe lives under tools/."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from threading import RLock
import unittest

from companion_mind.connector_alpha.contract import Binding, Denied, require
from companion_mind.connector_alpha.secret_store import CredentialBroker


def test_binding(**changes):
    b = Binding('synthetic-install', '123-test.apps.googleusercontent.com', 'test-project',
                'Synthetic test', 'fixture@example.invalid', 'synthetic-subject', 'owner', 'universe',
                'qualification', 'synthetic-file', 'S-1-5-21-123', '2099-01-01T00:00:00+00:00',
                True, True, True)
    return replace(b, **changes)


class MemoryStore:
    def __init__(self):
        self.data, self.lock = {}, RLock()
        self.fail_writes = self.fail_delete = False
    @contextmanager
    def mutex(self, target):
        with self.lock:
            yield
    def read_record(self, target, persist=1):
        item = self.data.get(target)
        if item is None:
            return None
        require(item[0] == persist, 'CREDENTIAL_BOUNDARY_DENIED')
        return deepcopy(item[1])
    def write_record(self, target, value, persist=1):
        require(not self.fail_writes, 'SECRETSTORE_WRITE_FAILED')
        self.data[target] = (persist, deepcopy(value))
    def write_access_token(self, target, value):
        self.write_record(target, {'access_token': value})
    def read(self, target):
        value = self.read_record(target)
        require(value is not None, 'CREDENTIAL_MISSING')
        return value['access_token']
    def delete(self, target):
        require(not self.fail_delete, 'SECRETSTORE_DELETE_FAILED')
        self.data.pop(target, None)


class Gate:
    locked = False
    def require_active(self):
        require(not self.locked, 'SESSION_LOCKED_OR_UNAVAILABLE')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.binding, self.store, self.gate = test_binding(), MemoryStore(), Gate()
        self.broker = self.new_broker()
    def new_broker(self):
        return CredentialBroker(self.binding, self.store, self.gate)
    def install(self):
        self.broker.install_access_token('SYNTHETIC_ONLY_RANDOM_TEST_VALUE')
    def test_read_requires_explicit_enrollment(self):
        with self.assertRaises(Denied):
            self.broker.begin_read()
    def test_exactly_one_read_across_brokers(self):
        self.install()
        reader = self.new_broker()
        reader.begin_read()
        self.assertTrue(reader.acquire().startswith('SYNTHETIC_ONLY_'))
        with self.assertRaises(Denied):
            self.new_broker().begin_read()
    def test_restart_of_reading_state_fails_closed(self):
        self.install()
        self.broker.begin_read()
        restored = self.new_broker()
        with self.assertRaises(Denied):
            restored.acquire()
        with self.assertRaises(Denied):
            self.broker.assert_active()
    def test_lock_closes_grant_no_unlock_revival(self):
        self.install()
        self.broker.begin_read()
        self.gate.locked = True
        with self.assertRaises(Denied):
            self.broker.assert_active()
        self.gate.locked = False
        with self.assertRaises(Denied):
            self.new_broker().begin_read()
    def test_cleanup_works_when_locked(self):
        self.install()
        self.gate.locked = True
        self.broker.close_local()
        self.assertTrue(self.broker.token_for_cleanup().startswith('SYNTHETIC_ONLY_'))
        self.assertTrue(self.broker.close_and_delete())
        self.assertIsNone(self.store.read_record(self.binding.credential_target))
    def test_failed_delete_does_not_reopen_grant(self):
        self.install()
        self.store.fail_delete = True
        with self.assertRaises(Denied):
            self.broker.close_and_delete()
        with self.assertRaises(Denied):
            self.new_broker().begin_read()
    def test_failed_lifecycle_write_cannot_issue_token(self):
        self.store.fail_writes = True
        with self.assertRaises(Denied):
            self.install()
        self.assertEqual({}, self.store.data)
    def test_corrupt_ledger_is_not_bootstrapped(self):
        self.store.data[self.binding.lifecycle_target] = (2, {'status': 'ACTIVE'})
        with self.assertRaises(Denied):
            self.install()
    def test_closed_regrant_increments_generation(self):
        self.install()
        self.broker.close_and_delete()
        next_broker = self.new_broker()
        next_broker.install_access_token('SYNTHETIC_ONLY_REGRANTED_TEST_VALUE')
        self.assertEqual(2, next_broker.lifecycle.generation)
        with self.assertRaises(Denied):
            self.broker.acquire()
    def test_unenrolled_binding_cannot_install(self):
        b = CredentialBroker(replace(self.binding, subject_permission_id=None), self.store, self.gate)
        with self.assertRaises(Denied):
            b.install_access_token('SYNTHETIC_ONLY_RANDOM_TEST_VALUE')


if __name__ == '__main__':
    unittest.main()
