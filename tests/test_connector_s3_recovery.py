from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from companion_mind.connector_s3.native import current_sid
from companion_mind.connector_s3.policy import Denied
from companion_mind.connector_s3.recovery import RecoveryStore, UserDPAPI


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI qualification only')
class NativeRecovery(unittest.TestCase):
    def test_no_alternative_protector_and_wrong_receipt_binding(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with self.assertRaisesRegex(Denied, 'RECOVERY_PROTECTOR_DENIED'):
                RecoveryStore(directory, 'a' * 64, object())
            store = RecoveryStore(directory, 'a' * 64, UserDPAPI(current_sid()))
            receipt = store.save('synthetic-op', b'synthetic-only')
            with self.assertRaisesRegex(Denied, 'RECOVERY_RECEIPT_INVALID'):
                store.load(replace(receipt, binding_hash='b' * 64))

    def test_missing_package_has_fixed_failure_code(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = RecoveryStore(directory, 'a' * 64, UserDPAPI(current_sid()))
            receipt = store.save('synthetic-op', b'synthetic-only')
            next(Path(directory).iterdir()).unlink()
            with self.assertRaisesRegex(Denied, '^RECOVERY_PACKAGE_UNAVAILABLE$'):
                store.load(receipt)

    def test_encrypted_package_reopens_and_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            binding_hash = hashlib.sha256(b'synthetic-binding').hexdigest()
            raw = b'only synthetic recovery content, never a real document'
            store = RecoveryStore(directory, binding_hash, UserDPAPI(current_sid()))
            receipt = store.save('synthetic-op', raw)
            paths = list(Path(directory).iterdir())
            self.assertEqual(len(paths), 1)
            cipher = paths[0].read_bytes()
            self.assertNotIn(raw, cipher)
            reopened = RecoveryStore(directory, binding_hash, UserDPAPI(current_sid()))
            self.assertEqual(reopened.load(receipt), raw)
            with self.assertRaises(FileExistsError):
                reopened.save('synthetic-op', b'cannot overwrite')
            with self.assertRaisesRegex(Denied, 'RECOVERY_PACKAGE_INVALID'):
                reopened.load(replace(receipt, plain_hash='0' * 64))
            paths[0].write_bytes(cipher[:-1] + bytes([cipher[-1] ^ 1]))
            with self.assertRaisesRegex(Denied, 'RECOVERY_PACKAGE_INVALID'):
                reopened.load(receipt)

    def test_wrong_binding_entropy_cannot_decrypt(self):
        dpapi = UserDPAPI(current_sid())
        a, b = hashlib.sha256(b'a').digest(), hashlib.sha256(b'b').digest()
        encrypted = dpapi.protect(b'synthetic', a)
        with self.assertRaisesRegex(Denied, 'RECOVERY_CRYPTO_FAILED'):
            dpapi.unprotect(encrypted, b)


if __name__ == '__main__':
    unittest.main()
