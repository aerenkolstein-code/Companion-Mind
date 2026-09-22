"""Purpose-limited S3 native token records; no HTTP, OAuth, logging, or CLI."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

from companion_mind.connector_alpha.secret_store import MAX_BLOB

from .native import PREFIX
from .policy import require
from .stage import GenerationBinding


def _binding_hash(binding):
    raw = json.dumps(asdict(binding), sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    return hashlib.sha256(raw).hexdigest()


def _record_hash(access, refresh, expiry):
    return hashlib.sha256((access + '\0' + refresh + '\0' + str(expiry)).encode('ascii')).hexdigest()


def _native_bytes(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


class NativeCredentials:
    """Expose only the token required by the caller's fixed purpose.

    ``session_gate`` is normally S2's ``SessionGate``. A callable is accepted
    only to make the same point-in-time gate testable without a Windows desktop.
    """
    def __init__(self, store, lifecycle, binding, target, session_gate):
        require(isinstance(binding, GenerationBinding) and type(target) is str
                and target.startswith(PREFIX) and target.endswith('.Tokens')
                and (callable(session_gate) or callable(getattr(session_gate, 'require_active', None))),
                'CREDENTIAL_CONFIGURATION_DENIED')
        self.store, self.lifecycle, self.binding = store, lifecycle, binding
        self.target, self.gate, self.binding_hash = target, session_gate, _binding_hash(binding)

    def rotate(self, access, refresh, expiry, version, *, now):
        """Commit an exact new version, leaving interrupted updates unreadable."""
        require(all(type(value) is str and 20 <= len(value) <= 2048
                    and all(33 <= ord(char) <= 126 for char in value)
                    for value in (access, refresh))
                and type(now) is int and type(expiry) is int and expiry > now
                and type(version) is int and version > 0,
                'CREDENTIAL_INVALID')
        record = {'access': access, 'refresh': refresh, 'expiry': expiry,
                  'version': version, 'binding': self.binding_hash}
        require(len(_native_bytes(record)) <= MAX_BLOB, 'CREDENTIAL_LIMIT')
        witness = {'phase': 'PREPARED', 'version': version, 'expiry': expiry,
                   'binding': self.binding_hash, 'hash': _record_hash(access, refresh, expiry)}
        with self.lifecycle.lock:
            self._session_active()
            self.lifecycle.assert_active(self.binding, now=now)
            with self.store.mutex(self.target):
                self._session_active()
                self.lifecycle.assert_active(self.binding, now=now)
                prior_record = self.store.read_record(self.target, persist=2)
                prior_witness = self.store.read_record(self.target + '.Meta', persist=2)
                if prior_record is None and prior_witness is None:
                    require(version == 1, 'CREDENTIAL_VERSION_DENIED')
                else:
                    require(prior_record is not None and prior_witness is not None, 'CREDENTIAL_RECOVERY_REQUIRED')
                    prior = self._committed(prior_record, prior_witness)
                    require(version == prior['version'] + 1, 'CREDENTIAL_VERSION_DENIED')
                self._session_active()
                self.lifecycle.assert_active(self.binding, now=now)
                self.store.write_record(self.target + '.Meta', witness, persist=2)
                require(self.store.read_record(self.target + '.Meta', persist=2) == witness,
                        'CREDENTIAL_READBACK_FAILED')
                self._session_active()
                self.lifecycle.assert_active(self.binding, now=now)
                self.store.write_record(self.target, record, persist=2)
                require(self.store.read_record(self.target, persist=2) == record,
                        'CREDENTIAL_READBACK_FAILED')
                self._session_active()
                self.lifecycle.assert_active(self.binding, now=now)
                witness['phase'] = 'COMMITTED'
                self.store.write_record(self.target + '.Meta', witness, persist=2)
                require(self.store.read_record(self.target + '.Meta', persist=2) == witness,
                        'CREDENTIAL_READBACK_FAILED')
                self._session_active()
                self.lifecycle.assert_active(self.binding, now=now)

    def business(self, now):
        return self._get('access', now, active=True)

    def refresh(self, now):
        return self._get('refresh', now, active=True)

    def cleanup(self, now):
        return self._get('refresh', now, active=False)

    def _session_active(self):
        if callable(self.gate):
            require(self.gate(), 'SESSION_GATE_DENIED')
        else:
            self.gate.require_active()

    def _get(self, purpose, now, active):
        require(type(now) is int, 'SESSION_GATE_DENIED')
        with self.lifecycle.lock:
            self._session_active()
            self._authorize(active, now)
            with self.store.mutex(self.target):
                record = self._committed(self.store.read_record(self.target, persist=2),
                                         self.store.read_record(self.target + '.Meta', persist=2))
                require(purpose != 'access' or now < record['expiry'], 'CREDENTIAL_RECOVERY_REQUIRED')
                self._session_active()
                self._authorize(active, now)
                return record[purpose]

    def _authorize(self, active, now):
        if active:
            self.lifecycle.assert_active(self.binding, now=now)
        else:
            self.lifecycle.assert_cleanup(self.binding, now=now)

    def _committed(self, record, witness):
        require(type(record) is dict and set(record) == {'access', 'refresh', 'expiry', 'version', 'binding'}
                and type(witness) is dict and set(witness) == {'phase', 'version', 'expiry', 'binding', 'hash'}
                and witness['phase'] == 'COMMITTED'
                and record['binding'] == witness['binding'] == self.binding_hash
                and record['version'] == witness['version'] and record['expiry'] == witness['expiry']
                and type(record['version']) is int and record['version'] > 0
                and type(record['expiry']) is int and record['expiry'] > 0
                and type(witness['version']) is int and witness['version'] > 0
                and type(witness['expiry']) is int and witness['expiry'] > 0
                and type(witness['hash']) is str and len(witness['hash']) == 64
                and all(type(record[name]) is str and 20 <= len(record[name]) <= 2048
                        and all(33 <= ord(char) <= 126 for char in record[name])
                        for name in ('access', 'refresh')), 'CREDENTIAL_RECOVERY_REQUIRED')
        require(_record_hash(record['access'], record['refresh'], record['expiry']) == witness['hash'],
                'CREDENTIAL_RECOVERY_REQUIRED')
        return record
