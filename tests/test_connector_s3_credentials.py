import copy
from contextlib import contextmanager
import os
import unittest
import uuid

from companion_mind.connector_alpha.secret_store import current_sid
from companion_mind.connector_s3.credentials import NativeCredentials
from companion_mind.connector_s3.native import PREFIX, S3CredentialStore
from companion_mind.connector_s3.policy import Denied, MemoryState
from companion_mind.connector_s3.stage import GenerationBinding, StageLifecycle


class Store:
    def __init__(self): self.data, self.fail_target = {}, None
    @contextmanager
    def mutex(self, _): yield
    def read_record(self, key, persist=2): return copy.deepcopy(self.data.get(key))
    def write_record(self, key, value, persist=2):
        if key == self.fail_target: raise Denied('INTERRUPTED_WRITE')
        self.data[key] = copy.deepcopy(value)


class Gate:
    def __init__(self, denial=None): self.denial, self.calls = denial, 0
    def require_active(self):
        self.calls += 1
        if self.denial: raise Denied(self.denial)


class RevokeAtCredentialLock(Store):
    def __init__(self): super().__init__(); self.hook = None
    @contextmanager
    def mutex(self, target):
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        yield


def binding(*, sid='sid'):
    return GenerationBinding(1, 'a', 's', 'u', 'c', 'p', 'drive.file', sid, 0, 100, 'f', 'A', 'B')


class Credentials(unittest.TestCase):
    target = PREFIX + 'Synthetic.Tokens'

    def setUp(self):
        self.b = binding(); self.stage = StageLifecycle(self.b, MemoryState()); self.stage.initialize(); self.s = Store()
        self.c = NativeCredentials(self.s, self.stage, self.b, self.target, lambda: True)

    def test_purpose_expiry_revoke_and_old_generation(self):
        self.c.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        self.assertEqual(self.c.business(1), 'a' * 20)
        self.assertEqual(self.c.refresh(1), 'r' * 20)
        self.assertEqual(self.c.refresh(50), 'r' * 20)
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_RECOVERY_REQUIRED'): self.c.business(50)
        self.stage.revoke_local(self.b)
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'): self.c.business(1)
        self.assertEqual(self.c.cleanup(50), 'r' * 20)
        with self.assertRaisesRegex(Denied, 'CLEANUP_DENIED'): self.c.cleanup(-1)

    def test_tamper_session_and_prepared_crash_fail_closed(self):
        self.c.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        self.s.data[self.target]['access'] = 'x' * 20
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_RECOVERY_REQUIRED'): self.c.business(1)
        blocked = NativeCredentials(self.s, self.stage, self.b, self.target, lambda: False)
        with self.assertRaisesRegex(Denied, 'SESSION_GATE_DENIED'): blocked.business(1)
        failed = Store(); failed.fail_target = self.target
        crash = NativeCredentials(failed, self.stage, self.b, self.target, lambda: True)
        with self.assertRaisesRegex(Denied, 'INTERRUPTED_WRITE'): crash.rotate('b' * 20, 'q' * 20, 50, 1, now=1)
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_RECOVERY_REQUIRED'): crash.business(1)

    def test_session_gate_object_checks_each_access(self):
        self.c.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        gate = Gate()
        checked = NativeCredentials(self.s, self.stage, self.b, self.target, gate)
        self.assertEqual(checked.business(1), 'a' * 20)
        self.assertEqual(checked.refresh(1), 'r' * 20)
        self.assertEqual(gate.calls, 4)
        with self.assertRaisesRegex(Denied, 'WINDOWS_IDENTITY_MISMATCH'):
            NativeCredentials(self.s, self.stage, self.b, self.target, Gate('WINDOWS_IDENTITY_MISMATCH')).business(1)

    def test_closed_generation_cannot_use_old_tokens(self):
        self.c.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        self.stage.close_generation(1)
        next_binding = GenerationBinding(2, 'a', 's', 'u', 'c', 'p', 'drive.file', 'sid', 100, 200, 'f', 'A', 'B')
        self.stage.start_next_generation(next_binding)
        with self.assertRaisesRegex(Denied, 'GENERATION_CLOSED'): self.c.business(1)

    def test_version_and_full_record_limits(self):
        self.c.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        self.c.rotate('b' * 20, 'q' * 20, 50, 2, now=1)
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_VERSION_DENIED'): self.c.rotate('c' * 20, 'z' * 20, 50, 2, now=1)
        too_large = NativeCredentials(Store(), self.stage, self.b, self.target, lambda: True)
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_LIMIT'): too_large.rotate('a' * 2048, 'r' * 2048, 50, 1, now=1)
        self.s.data[self.target + '.Meta']['version'] = 99
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_RECOVERY_REQUIRED'): self.c.business(1)

    def test_rotate_requires_active_stage_and_committed_previous_pair(self):
        before = copy.deepcopy(self.s.data)
        self.stage.revoke_local(self.b)
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'):
            self.c.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        self.assertEqual(self.s.data, before)

        b = binding(); stage = StageLifecycle(b, MemoryState()); stage.initialize(); store = Store()
        credentials = NativeCredentials(store, stage, b, self.target, lambda: True)
        credentials.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        store.data[self.target + '.Meta']['phase'] = 'PREPARED'
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_RECOVERY_REQUIRED'):
            credentials.rotate('b' * 20, 'q' * 20, 60, 2, now=1)
        self.assertEqual(store.data[self.target + '.Meta']['phase'], 'PREPARED')

    def test_rotation_requires_session_and_exact_version_chain(self):
        denied = NativeCredentials(self.s, self.stage, self.b, self.target, lambda: False)
        with self.assertRaisesRegex(Denied, 'SESSION_GATE_DENIED'):
            denied.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        self.assertEqual(self.s.data, {})
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_VERSION_DENIED'):
            self.c.rotate('a' * 20, 'r' * 20, 50, 2, now=1)
        self.c.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_VERSION_DENIED'):
            self.c.rotate('b' * 20, 'q' * 20, 60, 3, now=1)
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_INVALID'):
            self.c.rotate('b' * 20, 'q' * 20, 1, 2, now=1)

    def test_witness_boolean_or_float_fields_fail_closed(self):
        self.c.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        self.s.data[self.target + '.Meta']['version'] = True
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_RECOVERY_REQUIRED'): self.c.business(1)
        self.s.data[self.target + '.Meta']['version'] = 1
        self.s.data[self.target + '.Meta']['expiry'] = 50.0
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_RECOVERY_REQUIRED'): self.c.business(1)

    def test_revocation_at_credential_lock_never_returns_secret(self):
        b = binding(); stage = StageLifecycle(b, MemoryState()); stage.initialize(); store = RevokeAtCredentialLock()
        credentials = NativeCredentials(store, stage, b, self.target, lambda: True)
        credentials.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
        store.hook = lambda: stage.revoke_local(b)
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'): credentials.business(1)
        self.assertTrue(stage.snapshot()['revoked'])


@unittest.skipUnless(os.name == 'nt', 'Windows Credential Manager canary')
class WindowsCredentialCanary(unittest.TestCase):
    def test_new_uuid_slot_readback_and_cleanup(self):
        sid, target = current_sid(), PREFIX + 'Synthetic.' + str(uuid.uuid4()) + '.Tokens'
        store = S3CredentialStore(sid)
        b = binding(sid=sid); stage = StageLifecycle(b, MemoryState()); stage.initialize()
        credentials = NativeCredentials(store, stage, b, target, lambda: True)
        self.assertIsNone(store.read_record(target, persist=2))
        try:
            credentials.rotate('a' * 20, 'r' * 20, 50, 1, now=1)
            self.assertEqual(credentials.business(1), 'a' * 20)
            stage.revoke_local(b)
            self.assertEqual(credentials.cleanup(1), 'r' * 20)
        finally:
            store.delete(target)
            store.delete(target + '.Meta')
        self.assertIsNone(store.read_record(target, persist=2))
        self.assertIsNone(store.read_record(target + '.Meta', persist=2))
