"""Windows current-user secret storage; no plaintext or alternate credential fallback."""
from __future__ import annotations
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import json
import os
from threading import RLock
from .contract import Denied, exact_json, require

PREFIX = 'CompanionMind.ConnectorAlpha.'
MAX_BLOB = 2560

def _dll(name):
    require(os.name == 'nt', 'WINDOWS_SECRETSTORE_REQUIRED')
    return ctypes.WinDLL(name, use_last_error=True)

def _fn(lib, name, args, result):
    fn = getattr(lib, name)
    fn.argtypes, fn.restype = args, result
    return fn

def current_sid():
    k, a = _dll('kernel32'), _dll('advapi32')
    process = _fn(k, 'GetCurrentProcess', [], w.HANDLE)()
    open_token = _fn(a, 'OpenProcessToken', [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL)
    info = _fn(a, 'GetTokenInformation', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL)
    convert = _fn(a, 'ConvertSidToStringSidW', [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)], w.BOOL)
    close = _fn(k, 'CloseHandle', [w.HANDLE], w.BOOL)
    free = _fn(k, 'LocalFree', [ctypes.c_void_p], ctypes.c_void_p)
    token, needed = w.HANDLE(), w.DWORD()
    require(open_token(process, 8, ctypes.byref(token)), 'WINDOWS_IDENTITY_UNAVAILABLE')
    try:
        info(token, 1, None, 0, ctypes.byref(needed))
        require(0 < needed.value <= 65536, 'WINDOWS_IDENTITY_UNAVAILABLE')
        buf = ctypes.create_string_buffer(needed.value)
        require(info(token, 1, buf, len(buf), ctypes.byref(needed)), 'WINDOWS_IDENTITY_UNAVAILABLE')
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        result = w.LPWSTR()
        require(convert(sid_ptr, ctypes.byref(result)), 'WINDOWS_IDENTITY_UNAVAILABLE')
        try:
            return result.value
        finally:
            free(ctypes.cast(result, ctypes.c_void_p))
    finally:
        close(token)

class SessionGate:
    """Actual SID/session and input-desktop checks at dispatch and delivery.
    This is point-in-time gating, not continuous lock monitoring."""
    def __init__(self, sid=None):
        self.sid = sid if sid is not None else current_sid()
        self.k, self.u = _dll('kernel32'), _dll('user32')
        self.pid = _fn(self.k, 'GetCurrentProcessId', [], w.DWORD)
        self.session = _fn(self.k, 'ProcessIdToSessionId', [w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL)
        self.console = _fn(self.k, 'WTSGetActiveConsoleSessionId', [], w.DWORD)
        self.open_desktop = _fn(self.u, 'OpenInputDesktop', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
        self.info = _fn(self.u, 'GetUserObjectInformationW', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL)
        self.close_desktop = _fn(self.u, 'CloseDesktop', [w.HANDLE], w.BOOL)

    def require_identity(self):
        require(current_sid() == self.sid, 'WINDOWS_IDENTITY_MISMATCH')

    def require_active(self):
        self.require_identity()
        session, needed = w.DWORD(), w.DWORD()
        require(self.session(self.pid(), ctypes.byref(session)), 'SESSION_UNAVAILABLE')
        console = self.console()
        require(console != 0xFFFFFFFF and session.value == console, 'SESSION_UNAVAILABLE')
        desktop = self.open_desktop(0, False, 1)
        require(bool(desktop), 'SESSION_LOCKED_OR_UNAVAILABLE')
        try:
            name = ctypes.create_unicode_buffer(256)
            require(self.info(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed))
                    and name.value.lower() == 'default', 'SESSION_LOCKED_OR_UNAVAILABLE')
        finally:
            self.close_desktop(desktop)

class _Credential(ctypes.Structure):
    _fields_ = [('Flags', w.DWORD), ('Type', w.DWORD), ('TargetName', w.LPWSTR),
                ('Comment', w.LPWSTR), ('LastWritten', w.FILETIME),
                ('CredentialBlobSize', w.DWORD), ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)),
                ('Persist', w.DWORD), ('AttributeCount', w.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', w.LPWSTR), ('UserName', w.LPWSTR)]

class WindowsCredentialStore:
    def __init__(self, sid):
        self.sid = sid
        self.a, self.k = _dll('advapi32'), _dll('kernel32')
        self._read = _fn(self.a, 'CredReadW', [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))], w.BOOL)
        self._write = _fn(self.a, 'CredWriteW', [ctypes.POINTER(_Credential), w.DWORD], w.BOOL)
        self._delete = _fn(self.a, 'CredDeleteW', [w.LPCWSTR, w.DWORD, w.DWORD], w.BOOL)
        self._free = _fn(self.a, 'CredFree', [ctypes.c_void_p], None)
        self._create_mutex = _fn(self.k, 'CreateMutexW', [ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE)
        self._wait = _fn(self.k, 'WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD)
        self._release = _fn(self.k, 'ReleaseMutex', [w.HANDLE], w.BOOL)
        self._close = _fn(self.k, 'CloseHandle', [w.HANDLE], w.BOOL)

    def _target(self, target):
        require(type(target) is str and target.startswith(PREFIX) and len(target) <= 220
                and all(c.isalnum() or c in '.-' for c in target), 'CREDENTIAL_TARGET_DENIED')
        require(current_sid() == self.sid, 'WINDOWS_IDENTITY_MISMATCH')

    @contextmanager
    def mutex(self, target):
        self._target(target)
        handle = self._create_mutex(None, False, 'Global\\' + target)
        require(bool(handle), 'LIFECYCLE_LOCK_UNAVAILABLE')
        owned = False
        try:
            code = self._wait(handle, 20000)
            owned = code in (0, 0x80)
            require(code == 0, 'LIFECYCLE_LOCK_UNAVAILABLE')
            yield
        finally:
            if owned:
                self._release(handle)
            self._close(handle)

    def read_record(self, target, persist=1):
        self._target(target)
        ptr = ctypes.POINTER(_Credential)()
        if not self._read(target, 1, 0, ctypes.byref(ptr)):
            if ctypes.get_last_error() == 1168:
                return None
            raise Denied('SECRETSTORE_UNAVAILABLE')
        try:
            record = ptr.contents
            require(record.Persist == persist and record.UserName == self.sid
                    and record.TargetName == target and 0 < record.CredentialBlobSize <= MAX_BLOB,
                    'CREDENTIAL_BOUNDARY_DENIED')
            return exact_json(ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize))
        finally:
            if ptr and ptr.contents.CredentialBlob and ptr.contents.CredentialBlobSize <= MAX_BLOB:
                ctypes.memset(ptr.contents.CredentialBlob, 0, ptr.contents.CredentialBlobSize)
            self._free(ptr)

    def write_record(self, target, value, persist=1):
        self._target(target)
        require(persist in (1, 2), 'CREDENTIAL_PERSISTENCE_DENIED')
        raw = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
        require(0 < len(raw) <= MAX_BLOB, 'CREDENTIAL_LIMIT')
        blob = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
        record = _Credential()
        record.Type, record.TargetName, record.UserName, record.Persist = 1, target, self.sid, persist
        record.CredentialBlobSize = len(raw)
        record.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_byte))
        try:
            require(self._write(ctypes.byref(record), 0), 'SECRETSTORE_WRITE_FAILED')
        finally:
            ctypes.memset(blob, 0, len(raw))

    def write_access_token(self, target, token):
        require(type(token) is str and 20 <= len(token) <= 2048
                and all(33 <= ord(c) <= 126 for c in token), 'CREDENTIAL_INVALID')
        self.write_record(target, {'access_token': token})

    def read(self, target):
        value = self.read_record(target)
        require(value is not None, 'CREDENTIAL_MISSING')
        require(type(value) is dict and set(value) == {'access_token'}, 'CREDENTIAL_INVALID')
        token = value['access_token']
        require(type(token) is str and 20 <= len(token) <= 2048
                and all(33 <= ord(c) <= 126 for c in token), 'CREDENTIAL_INVALID')
        return token

    def delete(self, target):
        self._target(target)
        if not self._delete(target, 1, 0):
            require(ctypes.get_last_error() == 1168, 'SECRETSTORE_DELETE_FAILED')
        require(self.read_record(target) is None, 'SECRETSTORE_DELETE_FAILED')

class CredentialBroker:
    def __init__(self, binding, store=None, gate=None):
        from .lifecycle import Lifecycle
        self.binding = binding
        self.store = store if store is not None else WindowsCredentialStore(binding.windows_sid)
        self.gate = gate if gate is not None else SessionGate(binding.windows_sid)
        self.closed, self._lock = False, RLock()
        self.lifecycle = Lifecycle(binding, self.store)

    @contextmanager
    def synchronized(self):
        with self._lock, self.store.mutex(self.binding.lifecycle_target):
            yield

    def _active(self):
        require(not self.closed, 'LOCAL_AUTHORIZATION_CLOSED')
        try:
            self.binding.active()
            self.gate.require_active()
            self.lifecycle.check_read()
        except Exception:
            self.closed = True
            try:
                self.lifecycle.close()
            except Exception:
                pass
            raise

    def assert_active(self):
        with self.synchronized():
            self._active()

    def begin_read(self):
        with self.synchronized():
            require(not self.closed, 'LOCAL_AUTHORIZATION_CLOSED')
            try:
                self.binding.active()
                self.gate.require_active()
                self.lifecycle.begin_read()
            except Exception:
                self.closed = True
                try:
                    self.lifecycle.close()
                except Exception:
                    pass
                raise

    def acquire(self):
        with self.synchronized():
            self._active()
            return self.store.read(self.binding.credential_target)

    def desktop_client_secret(self):
        """Read only this binding's imported Desktop client, with no fallback."""
        with self.synchronized():
            require(not self.closed, 'LOCAL_AUTHORIZATION_CLOSED')
            self.binding.active(require_enrolled=False)
            self.gate.require_active()
            record = installed = secret = None
            try:
                target = PREFIX + 'DesktopClient.' + self.binding.project_id
                record = self.store.read_record(target, persist=2)
                require(type(record) is dict and set(record) == {'installed'},
                        'DESKTOP_CLIENT_CONFIG_MISSING_OR_INVALID')
                installed = record['installed']
                require(type(installed) is dict
                        and installed.get('client_id') == self.binding.client_id
                        and installed.get('project_id') == self.binding.project_id
                        and installed.get('token_uri') == 'https://oauth2.googleapis.com/token',
                        'DESKTOP_CLIENT_BINDING_MISMATCH')
                secret = installed.get('client_secret')
                require(type(secret) is str and 20 <= len(secret) <= 2048
                        and all(33 <= ord(c) <= 126 for c in secret),
                        'DESKTOP_CLIENT_SECRET_MISSING_OR_INVALID')
                self.binding.active(require_enrolled=False)
                self.gate.require_active()
                return secret
            finally:
                record = installed = secret = None

    def install_access_token(self, token, expires_in=3600):
        with self.synchronized():
            require(not self.closed, 'LOCAL_AUTHORIZATION_CLOSED')
            self.binding.active()
            self.gate.require_active()
            self.lifecycle.prepare(expires_in)
            try:
                self.store.write_access_token(self.binding.credential_target, token)
                self.lifecycle.activate()
            except Exception:
                self.closed = True
                try:
                    self.lifecycle.close()
                finally:
                    self.store.delete(self.binding.credential_target)
                raise

    def close_local(self):
        self.closed = True
        with self.synchronized():
            self.lifecycle.close()

    def token_for_cleanup(self):
        require(self.closed, 'CLEANUP_REQUIRES_LOCAL_CLOSE')
        with self.synchronized():
            return self.store.read(self.binding.credential_target)

    def close_and_delete(self):
        try:
            self.close_local()
        finally:
            with self.synchronized():
                self.store.delete(self.binding.credential_target)
        return True
