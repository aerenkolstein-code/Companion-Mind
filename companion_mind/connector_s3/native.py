"""S3 native secrets and serialized, crash-evident non-secret state.

The native witness detects stale files, not privileged rollback of both Windows
Credential Manager and the filesystem. No provider operations are implemented.
"""
from __future__ import annotations
import hashlib
import ctypes
import json
import os
from pathlib import Path
import tempfile
from threading import local
from companion_mind.connector_alpha.secret_store import WindowsCredentialStore, current_sid
from .policy import Denied, require

PREFIX = 'CompanionMind.ConnectorS3.'
MAX_LEDGER_BYTES = 1024 * 1024


class S3CredentialStore(WindowsCredentialStore):
    """Reuse Win32 primitives only; reject every S2 target."""
    def _target(self, target):
        require(type(target) is str and target.startswith(PREFIX) and len(target) <= 220
                and all(c.isalnum() or c in '.-' for c in target), 'S3_CREDENTIAL_TARGET_DENIED')
        require(current_sid() == self.sid, 'WINDOWS_IDENTITY_MISMATCH')

    def write_tokens(self, target, access, refresh):
        require(all(type(v) is str and 20 <= len(v) <= 1024 and all(33 <= ord(c) <= 126 for c in v)
                    for v in (access, refresh)), 'S3_CREDENTIAL_INVALID')
        self.write_record(target, {'access_token': access, 'refresh_token': refresh}, persist=2)

    def read_tokens(self, target):
        value = self.read_record(target, persist=2)
        require(type(value) is dict and set(value) == {'access_token', 'refresh_token'}
                and all(type(v) is str and 20 <= len(v) <= 1024 and all(33 <= ord(c) <= 126 for c in v)
                        for v in value.values()), 'S3_CREDENTIAL_INVALID')
        return value

    def delete(self, target):
        self._target(target)
        if not self._delete(target, 1, 0):
            require(ctypes.get_last_error() == 1168, 'SECRETSTORE_DELETE_FAILED')
        require(self.read_record(target, persist=2) is None, 'SECRETSTORE_DELETE_FAILED')


class _StoreLock:
    """Reusable context stack for the reentrant Windows mutex."""
    def __init__(self, store, target):
        self.store, self.target, self.local = store, target, local()

    def __enter__(self):
        context = self.store.mutex(self.target)
        context.__enter__()
        if not hasattr(self.local, 'stack'):
            self.local.stack = []
        self.local.stack.append(context)
        return self

    def __exit__(self, *args):
        return self.local.stack.pop().__exit__(*args)


def _json(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            require(key not in out, 'RECOVERY_REQUIRED')
            out[key] = value
        return out
    def reject_constant(_):
        raise Denied('RECOVERY_REQUIRED')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)


class NativeLedger:
    """Native PREPARED precedes disk mutation. Interrupted commits stay closed.

    Callers hold lock for a complete read/modify/write transaction; Controller
    adopts that same mutex. Reading never initializes or repairs missing state.
    """
    def __init__(self, directory, store, target, stage):
        require(type(target) is str and target.startswith(PREFIX), 'S3_CREDENTIAL_TARGET_DENIED')
        require(type(stage) is str and 1 <= len(stage) <= 128, 'STAGE_INVALID')
        self.dir = Path(directory).resolve()
        self.state = self.dir / 'state.json'
        self.store, self.target, self.stage = store, target, stage
        self.path_hash = hashlib.sha256(os.path.normcase(str(self.state)).encode('utf-8')).hexdigest()
        self.lock = _StoreLock(store, target)

    def initialize(self, data):
        with self.lock:
            require(not self.state.exists() and self.store.read_record(self.target, persist=2) is None,
                    'LEDGER_ALREADY_EXISTS')
            self._commit(data, 1)

    def read(self):
        with self.lock:
            try:
                witness = self.store.read_record(self.target, persist=2)
                require(type(witness) is dict and set(witness) == {'phase', 'sequence', 'sha256', 'stage', 'path'}
                        and witness['phase'] == 'COMMITTED' and witness['stage'] == self.stage
                        and witness['path'] == self.path_hash and type(witness['sequence']) is int
                        and witness['sequence'] > 0, 'RECOVERY_REQUIRED')
                require(self.state.stat().st_size <= MAX_LEDGER_BYTES, 'RECOVERY_REQUIRED')
                raw = self.state.read_bytes()
                require(hashlib.sha256(raw).hexdigest() == witness['sha256'], 'RECOVERY_REQUIRED')
                data = _json(raw)
                require(type(data) is dict, 'RECOVERY_REQUIRED')
                return data
            except (OSError, ValueError, TypeError, KeyError):
                raise Denied('RECOVERY_REQUIRED') from None

    def write(self, data):
        with self.lock:
            self.read()
            witness = self.store.read_record(self.target, persist=2)
            self._commit(data, witness['sequence'] + 1)

    def _commit(self, data, sequence):
        require(type(data) is dict, 'LEDGER_INVALID')
        raw = json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        require(len(raw) <= MAX_LEDGER_BYTES, 'LEDGER_LIMIT')
        witness = {'phase': 'PREPARED', 'sequence': sequence, 'sha256': hashlib.sha256(raw).hexdigest(),
                   'stage': self.stage, 'path': self.path_hash}
        self.store.write_record(self.target, witness, persist=2)
        require(self.store.read_record(self.target, persist=2) == witness, 'LEDGER_READBACK_FAILED')
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.s3-', dir=self.dir)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.state)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        require(self.state.read_bytes() == raw, 'LEDGER_READBACK_FAILED')
        witness['phase'] = 'COMMITTED'
        self.store.write_record(self.target, witness, persist=2)
        require(self.store.read_record(self.target, persist=2) == witness and self.read() == data,
                'LEDGER_READBACK_FAILED')
