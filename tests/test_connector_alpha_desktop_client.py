"""Synthetic-only tests for the exact imported Desktop client boundary."""
from copy import deepcopy
from dataclasses import replace
import unittest

from companion_mind.connector_alpha.contract import Denied
from companion_mind.connector_alpha.secret_store import CredentialBroker, PREFIX
from test_connector_alpha_lifecycle import MemoryStore, Gate, test_binding

SECRET = 'SYNTHETIC_ONLY_DESKTOP_CLIENT_SECRET'


class DesktopClientBoundary(unittest.TestCase):
    def setUp(self):
        self.binding = test_binding(subject_permission_id=None)
        self.store, self.gate = MemoryStore(), Gate()
        self.target = PREFIX + 'DesktopClient.' + self.binding.project_id
        self.record = {'installed': {'client_id': self.binding.client_id,
                       'project_id': self.binding.project_id,
                       'token_uri': 'https://oauth2.googleapis.com/token',
                       'client_secret': SECRET}}
        self.store.write_record(self.target, self.record, persist=2)

    def broker(self, binding=None):
        return CredentialBroker(binding or self.binding, self.store, self.gate)

    def test_exact_client_can_be_read_before_enrollment_without_grant_write(self):
        before = deepcopy(self.store.data)
        self.assertEqual(self.broker().desktop_client_secret(), SECRET)
        self.assertEqual(self.store.data, before)
        self.assertNotIn(self.binding.lifecycle_target, self.store.data)

    def test_missing_exact_record_cannot_fall_back_to_other_client(self):
        self.store.data[self.target + '-other'] = self.store.data.pop(self.target)
        with self.assertRaisesRegex(Denied, 'DESKTOP_CLIENT_CONFIG_MISSING_OR_INVALID'):
            self.broker().desktop_client_secret()

    def test_project_client_and_token_destination_must_match(self):
        for key, wrong in (('project_id', 'other-project'), ('client_id', '456-other.apps.googleusercontent.com'),
                           ('token_uri', 'https://example.invalid/token')):
            with self.subTest(key=key):
                record = deepcopy(self.record)
                record['installed'][key] = wrong
                self.store.write_record(self.target, record, persist=2)
                with self.assertRaisesRegex(Denied, 'DESKTOP_CLIENT_BINDING_MISMATCH'):
                    self.broker().desktop_client_secret()

    def test_invalid_secret_is_never_echoed_or_returned(self):
        for value in (None, '', 'short', 'x' * 2049, 'secret\nwith-control-character', {'secret': SECRET}):
            with self.subTest(value_type=type(value).__name__):
                record = deepcopy(self.record)
                record['installed']['client_secret'] = value
                self.store.write_record(self.target, record, persist=2)
                with self.assertRaises(Denied) as caught:
                    self.broker().desktop_client_secret()
                self.assertEqual(str(caught.exception), 'DESKTOP_CLIENT_SECRET_MISSING_OR_INVALID')
                self.assertNotIn(SECRET, str(caught.exception))

    def test_expired_locked_closed_or_wrong_persistence_cannot_read(self):
        with self.assertRaisesRegex(Denied, 'BINDING_EXPIRED'):
            self.broker(replace(self.binding, expires_at='2000-01-01T00:00:00+00:00')).desktop_client_secret()
        self.gate.locked = True
        with self.assertRaisesRegex(Denied, 'SESSION_LOCKED_OR_UNAVAILABLE'):
            self.broker().desktop_client_secret()
        self.gate.locked = False
        broker = self.broker()
        broker.closed = True
        with self.assertRaisesRegex(Denied, 'LOCAL_AUTHORIZATION_CLOSED'):
            broker.desktop_client_secret()
        self.store.write_record(self.target, self.record, persist=1)
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_BOUNDARY_DENIED'):
            self.broker().desktop_client_secret()

    def test_lock_during_read_prevents_secret_return(self):
        read_record = self.store.read_record
        def read_and_lock(target, persist=1):
            result = read_record(target, persist)
            self.gate.locked = True
            return result
        self.store.read_record = read_and_lock
        with self.assertRaisesRegex(Denied, 'SESSION_LOCKED_OR_UNAVAILABLE'):
            self.broker().desktop_client_secret()


if __name__ == '__main__':
    unittest.main()
