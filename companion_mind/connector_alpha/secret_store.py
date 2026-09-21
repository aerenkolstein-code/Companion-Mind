"""Windows Credential Manager broker for Connector Alpha.

There is intentionally no file, environment, browser-cookie, or connector
fallback.  The only credential accepted by this module is an opaque JSON blob
stored in the current Windows user's non-roaming Credential Manager session.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os

from .contract import Denied, require

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_SESSION = 1
_ERROR_NOT_FOUND = 1168
_MAX_BLOB = 4096


class _Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', ctypes.c_byte * 8), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


class WindowsCredentialStore:
    """Small native store adapter. It deliberately refuses non-Windows hosts."""
    def __init__(self, sid):
        require(os.name == 'nt', 'WINDOWS_SECRETSTORE_REQUIRED')
        self.sid = sid
        self._advapi = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        self._advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                           ctypes.POINTER(ctypes.POINTER(_Credential))]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL

    def write_access_token(self, target, token):
        require(type(token) is str and 1 <= len(token) <= 3072, 'CREDENTIAL_INVALID')
        raw = json.dumps({'access_token': token}, separators=(',', ':')).encode('utf-8')
        require(len(raw) <= _MAX_BLOB, 'CREDENTIAL_INVALID')
        blob = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
        credential = _Credential()
        credential.Type, credential.TargetName = _CRED_TYPE_GENERIC, target
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_byte))
        credential.Persist, credential.UserName = _CRED_PERSIST_SESSION, self.sid
        if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
            raise Denied('SECRETSTORE_WRITE_FAILED')

    def read(self, target):
        ptr = ctypes.POINTER(_Credential)()
        if not self._advapi.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            code = ctypes.get_last_error()
            raise Denied('CREDENTIAL_MISSING' if code == _ERROR_NOT_FOUND else 'SECRETSTORE_UNAVAILABLE')
        try:
            record = ptr.contents
            require(record.Persist == _CRED_PERSIST_SESSION and record.UserName == self.sid,
                    'CREDENTIAL_BOUNDARY_DENIED')
            require(0 < record.CredentialBlobSize <= _MAX_BLOB, 'CREDENTIAL_INVALID')
            raw = ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
            try:
                value = json.loads(raw.decode('utf-8'))
            except (UnicodeError, ValueError):
                raise Denied('CREDENTIAL_INVALID') from None
            require(type(value) is dict and set(value) == {'access_token'}, 'CREDENTIAL_INVALID')
            require(type(value['access_token']) is str and 1 <= len(value['access_token']) <= 3072,
                    'CREDENTIAL_INVALID')
            return value['access_token']
        finally:
            self._advapi.CredFree(ptr)

    def delete(self, target):
        if not self._advapi.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
            code = ctypes.get_last_error()
            if code != _ERROR_NOT_FOUND:
                raise Denied('SECRETSTORE_DELETE_FAILED')


class SessionGate:
    """Fail closed unless the caller is in the active Windows console session."""
    def __init__(self):
        require(os.name == 'nt', 'WINDOWS_SECRETSTORE_REQUIRED')
        self._kernel = ctypes.WinDLL('Kernel32.dll', use_last_error=True)
        self._wts = ctypes.WinDLL('Wtsapi32.dll', use_last_error=True)

    def require_active(self):
        session = self._kernel.WTSGetActiveConsoleSessionId()
        require(session != 0xFFFFFFFF, 'SESSION_LOCKED_OR_UNAVAILABLE')
        # WTSConnectState: 0 is WTSActive. No best-effort alternative exists.
        state = wintypes.DWORD()
        buffer = ctypes.c_void_p()
        size = wintypes.DWORD()
        ok = self._wts.WTSQuerySessionInformationW(None, session, 8, ctypes.byref(buffer), ctypes.byref(size))
        if not ok:
            raise Denied('SESSION_LOCKED_OR_UNAVAILABLE')
        try:
            require(size.value >= ctypes.sizeof(state), 'SESSION_LOCKED_OR_UNAVAILABLE')
            state = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
            require(state == 0, 'SESSION_LOCKED_OR_UNAVAILABLE')
        finally:
            self._wts.WTSFreeMemory(buffer)


class CredentialBroker:
    """One binding, one current-session slot, with an injectable test gate."""
    def __init__(self, binding, store=None, gate=None):
        self.binding = binding
        self.store = store if store is not None else WindowsCredentialStore(binding.windows_sid)
        self.gate = gate if gate is not None else SessionGate()
        self.closed = False

    def acquire(self):
        require(not self.closed, 'LOCAL_AUTHORIZATION_CLOSED')
        self.gate.require_active()
        self.binding.active()
        return self.store.read(self.binding.credential_target)

    def install_access_token(self, token):
        require(not self.closed, 'LOCAL_AUTHORIZATION_CLOSED')
        self.gate.require_active()
        self.store.write_access_token(self.binding.credential_target, token)

    def close_and_delete(self):
        self.closed = True
        self.store.delete(self.binding.credential_target)
