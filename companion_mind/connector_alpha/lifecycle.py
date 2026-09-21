"""Nonsecret machine-local Credential Manager ledger, with one-use read grants.

A fresh process cannot resume READING. No ordinary-file state rollback path.
This trusts the Windows user/admin; it is not a hostile-user sandbox.
"""
import time
import uuid
from .contract import require


class Lifecycle:
    def __init__(self, binding, store):
        self.binding, self.store = binding, store
        self.owner = uuid.uuid4().hex
        self.generation = None

    def _load(self):
        data = self.store.read_record(self.binding.lifecycle_target, persist=2)
        if data is None:
            return None
        keys = {'format', 'binding', 'generation', 'status', 'owner', 'expires_at'}
        require(type(data) is dict and set(data) == keys and data['format'] == 'ca-ledger/1'
                and data['binding'] == self.binding.digest
                and type(data['generation']) is int and data['generation'] > 0
                and data['status'] in {'PREPARING', 'ACTIVE', 'READING', 'CLOSED'}
                and type(data['owner']) is str
                and type(data['expires_at']) in (int, float)
                and 0 <= data['expires_at'] < 1e12, 'LIFECYCLE_INVALID')
        return data

    def _save(self, data):
        self.store.write_record(self.binding.lifecycle_target, data, persist=2)
        require(self._load() == data, 'LIFECYCLE_READBACK_FAILED')

    def prepare(self, expires_in):
        require(type(expires_in) is int and 1 <= expires_in <= 3600, 'TOKEN_EXPIRY_INVALID')
        prior = self._load()
        require(prior is None or prior['status'] == 'CLOSED', 'PRIOR_GRANT_REQUIRES_CLEANUP')
        require(self.store.read_record(self.binding.credential_target) is None, 'PRIOR_GRANT_REQUIRES_CLEANUP')
        generation = prior['generation'] + 1 if prior else 1
        self._save({'format': 'ca-ledger/1', 'binding': self.binding.digest,
                    'generation': generation, 'status': 'PREPARING', 'owner': self.owner,
                    'expires_at': time.time() + expires_in})
        self.generation = generation

    def activate(self):
        data = self._load()
        require(data is not None and data['status'] == 'PREPARING' and data['owner'] == self.owner
                and data['generation'] == self.generation, 'LIFECYCLE_INVALID')
        data['status'] = 'ACTIVE'
        self._save(data)

    def begin_read(self):
        data = self._load()
        require(data is not None and data['status'] == 'ACTIVE' and data['expires_at'] > time.time(),
                'GRANT_NOT_ACTIVE')
        data.update(status='READING', owner=self.owner)
        self._save(data)
        self.generation = data['generation']

    def check_read(self):
        data = self._load()
        require(data is not None and data['status'] == 'READING' and data['owner'] == self.owner
                and data['generation'] == self.generation and data['expires_at'] > time.time(),
                'GRANT_NOT_ACTIVE')

    def close(self):
        data = self._load()
        if data is None:
            data = {'format': 'ca-ledger/1', 'binding': self.binding.digest, 'generation': 1,
                    'status': 'CLOSED', 'owner': '', 'expires_at': 0}
        else:
            data.update(status='CLOSED', owner='')
        self._save(data)
