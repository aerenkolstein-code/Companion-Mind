"""Current-user DPAPI recovery bytes; never a plaintext recovery fallback.

Receipts must be pinned in the native-witnessed stage ledger before any write.
This module neither authorizes writes nor reads provider content by itself.
"""
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from .native import current_sid
from .policy import require

MAX_PACKAGE = 2 * 1024 * 1024


class _Blob(ctypes.Structure):
    _fields_ = [('size', w.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


class UserDPAPI:
    def __init__(self, sid):
        require(os.name == 'nt', 'WINDOWS_DPAPI_REQUIRED')
        self.sid = sid
        self.crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.free = self.kernel.LocalFree
        self.free.argtypes, self.free.restype = [ctypes.c_void_p], ctypes.c_void_p
        for name in ('CryptProtectData', 'CryptUnprotectData'):
            function = getattr(self.crypt, name)
            function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                                 ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.POINTER(_Blob)]
            function.restype = w.BOOL

    def _run(self, name, raw, entropy):
        require(current_sid() == self.sid, 'WINDOWS_IDENTITY_MISMATCH')
        require(type(raw) is bytes and 0 < len(raw) <= MAX_PACKAGE + 65536
                and type(entropy) is bytes and len(entropy) == 32, 'RECOVERY_PACKAGE_INVALID')
        source_bytes = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
        entropy_bytes = (ctypes.c_byte * len(entropy)).from_buffer_copy(entropy)
        source, extra, result = _Blob(len(raw), source_bytes), _Blob(len(entropy), entropy_bytes), _Blob()
        try:
            # UI_FORBIDDEN only: intentionally never LOCAL_MACHINE.
            require(getattr(self.crypt, name)(ctypes.byref(source), None, ctypes.byref(extra), None,
                                             None, 1, ctypes.byref(result)), 'RECOVERY_CRYPTO_FAILED')
            require(bool(result.data) and 0 < result.size <= MAX_PACKAGE + 65536, 'RECOVERY_PACKAGE_INVALID')
            return ctypes.string_at(result.data, result.size)
        finally:
            ctypes.memset(source_bytes, 0, len(raw))
            ctypes.memset(entropy_bytes, 0, len(entropy))
            if result.data:
                if result.size <= MAX_PACKAGE + 65536:
                    ctypes.memset(result.data, 0, result.size)
                self.free(result.data)

    def protect(self, raw, entropy):
        return self._run('CryptProtectData', raw, entropy)

    def unprotect(self, raw, entropy):
        return self._run('CryptUnprotectData', raw, entropy)


@dataclass(frozen=True)
class RecoveryReceipt:
    operation_id: str
    cipher_hash: str
    plain_hash: str
    cipher_size: int


class RecoveryStore:
    def __init__(self, directory, binding_hash, protector):
        require(type(binding_hash) is str and len(binding_hash) == 64
                and all(c in '0123456789abcdef' for c in binding_hash), 'BINDING_HASH_INVALID')
        self.directory = Path(directory).resolve()
        self.binding_hash, self.protector = binding_hash, protector

    def _location(self, operation_id):
        require(type(operation_id) is str and 0 < len(operation_id) <= 128
                and all(c.isascii() and (c.isalnum() or c in '-_.') for c in operation_id), 'OPERATION_INVALID')
        entropy = hashlib.sha256((self.binding_hash + ':' + operation_id).encode('ascii')).digest()
        return self.directory / (entropy.hex() + '.dpapi'), entropy

    def save(self, operation_id, raw):
        require(type(raw) is bytes and 0 < len(raw) <= MAX_PACKAGE, 'RECOVERY_PACKAGE_INVALID')
        path, entropy = self._location(operation_id)
        cipher = self.protector.protect(raw, entropy)
        require(type(cipher) is bytes and 0 < len(cipher) <= MAX_PACKAGE + 65536, 'RECOVERY_PACKAGE_INVALID')
        self.directory.mkdir(parents=True, exist_ok=True)
        # An interrupted package remains unusable and cannot be overwritten.
        with path.open('xb') as stream:
            stream.write(cipher)
            stream.flush()
            os.fsync(stream.fileno())
        receipt = RecoveryReceipt(operation_id, _digest(cipher), _digest(raw), len(cipher))
        require(self.load(receipt) == raw, 'RECOVERY_READBACK_FAILED')
        return receipt

    def load(self, receipt):
        require(type(receipt) is RecoveryReceipt, 'RECOVERY_RECEIPT_INVALID')
        path, entropy = self._location(receipt.operation_id)
        require(type(receipt.cipher_size) is int and 0 < receipt.cipher_size <= MAX_PACKAGE + 65536
                and path.stat().st_size == receipt.cipher_size, 'RECOVERY_PACKAGE_INVALID')
        cipher = path.read_bytes()
        require(_digest(cipher) == receipt.cipher_hash, 'RECOVERY_PACKAGE_INVALID')
        plain = self.protector.unprotect(cipher, entropy)
        require(type(plain) is bytes and 0 < len(plain) <= MAX_PACKAGE
                and _digest(plain) == receipt.plain_hash, 'RECOVERY_PACKAGE_INVALID')
        return plain
