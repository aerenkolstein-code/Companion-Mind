"""Durable, fail-closed S3 counters and exact-resource policy.

This module deliberately has no OAuth, HTTP, filesystem scanning, or provider
adapter.  A caller must reserve before it can dispatch a provider request.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from threading import RLock


class Denied(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise Denied(code)


@dataclass(frozen=True)
class Limits:
    oauth: int = 4
    refresh: int = 6
    api_total: int = 240
    api_normal: int = 168
    api_safety: int = 72
    writes_a: int = 8
    writes_b: int = 8
    writes_c: int = 8
    nonrollbacks_a: int = 5
    nonrollbacks_b: int = 5
    nonrollbacks_c: int = 6
    rollbacks_a: int = 3
    rollbacks_b: int = 3
    rollbacks_c: int = 2
    creates_c: int = 1
    ordinary_buckets: tuple = (('initialization', 30), ('A_B_flow', 50), ('C_flow', 40),
                               ('continuity', 28), ('external_revoke_and_repair', 20))
    safety_buckets: tuple = (('restore_existing_and_readback', 40), ('C_cleanup_and_readback', 12),
                             ('revoke', 4), ('exact_unknown_reconciliation', 16))

    def __post_init__(self):
        require(self.api_normal + self.api_safety == self.api_total, 'LIMITS_INVALID')
        require(all(type(v) is int and v >= 0 for k, v in asdict(self).items()
                    if k not in {'ordinary_buckets', 'safety_buckets'}), 'LIMITS_INVALID')
        require(sum(v for _, v in self.ordinary_buckets) == self.api_normal
                and sum(v for _, v in self.safety_buckets) == self.api_safety
                and len({k for k, _ in self.ordinary_buckets + self.safety_buckets}) == len(self.ordinary_buckets + self.safety_buckets)
                and all(type(k) is str and type(v) is int and v >= 0
                        for k, v in self.ordinary_buckets + self.safety_buckets), 'LIMITS_INVALID')

    def buckets(self, lane): return dict(self.ordinary_buckets if lane == 'normal' else self.safety_buckets)


@dataclass(frozen=True)
class Binding:
    generation: int
    folder_id: str
    a_id: str
    b_id: str
    client_id: str
    project_id: str
    sid: str

    def __post_init__(self):
        values = asdict(self)
        require(type(self.generation) is int and self.generation > 0, 'BINDING_INVALID')
        for key, value in values.items():
            if key != 'generation':
                require(type(value) is str and 1 <= len(value) <= 180 and value.strip() == value,
                        'BINDING_INVALID')
        require(len({self.folder_id, self.a_id, self.b_id}) == 3, 'BINDING_INVALID')


class MemoryState:
    """Synthetic stand-in for the native non-secret persistent state store."""
    def __init__(self): self.raw = None
    def read(self): return self.raw
    def write(self, value): self.raw = json.loads(json.dumps(value, sort_keys=True))


class Controller:
    FORMAT = 'connector-s3/1'
    def __init__(self, binding, store, limits=Limits()):
        self.binding, self.store, self.limits, self.lock = binding, store, limits, RLock()

    def initialize(self):
        with self.lock:
            require(self.store.read() is None, 'LEDGER_ALREADY_EXISTS')
            self.store.write({'format': self.FORMAT, 'binding': asdict(self.binding),
                'revocation_epoch': 0, 'revoked': False, 'api_total': 0, 'api_normal': 0,
                'api_safety': 0, 'oauth': 0, 'refresh': 0,
                'writes': {'A': 0, 'B': 0, 'C': 0},
                'rollbacks': {'A': 0, 'B': 0, 'C': 0}, 'nonrollbacks': {'A': 0, 'B': 0, 'C': 0}, 'creates_c': 0,
                'buckets': {k: 0 for k in dict(self.limits.ordinary_buckets + self.limits.safety_buckets)},
                'operations': {}})

    def _load(self):
        data = self.store.read()
        require(type(data) is dict and data.get('format') == self.FORMAT
                and data.get('binding') == asdict(self.binding), 'RECOVERY_REQUIRED')
        required = {'format', 'binding', 'revocation_epoch', 'revoked', 'api_total', 'api_normal',
                    'api_safety', 'oauth', 'refresh', 'writes', 'rollbacks', 'nonrollbacks', 'creates_c', 'buckets', 'operations'}
        require(set(data) == required and type(data['revoked']) is bool
                and type(data['revocation_epoch']) is int and data['revocation_epoch'] >= 0
                and all(type(data[k]) is int and data[k] >= 0 for k in
                        ('api_total', 'api_normal', 'api_safety', 'oauth', 'refresh', 'creates_c')),
                'RECOVERY_REQUIRED')
        require(set(data['writes']) == {'A','B','C'} and set(data['rollbacks']) == {'A','B','C'} and set(data['nonrollbacks']) == {'A','B','C'}
                and all(type(v) is int and v >= 0 for d in (data['writes'], data['rollbacks'], data['nonrollbacks']) for v in d.values())
                and type(data['operations']) is dict and set(data['buckets']) == set(dict(self.limits.ordinary_buckets + self.limits.safety_buckets))
                and all(type(v) is int and v >= 0 for v in data['buckets'].values()), 'RECOVERY_REQUIRED')
        return data

    def _save(self, data):
        self.store.write(data)
        require(self.store.read() == data, 'LEDGER_READBACK_FAILED')

    def _live(self, data): require(not data['revoked'], 'GRANT_REVOKED')

    def start_oauth(self, operation_id):
        """Charge a browser/listener start before either component is launched."""
        with self.lock:
            data = self._load(); self._live(data)
            require(type(operation_id) is str and operation_id not in data['operations'], 'OPERATION_REPLAY')
            require(data['oauth'] < self.limits.oauth, 'OAUTH_BUDGET_EXHAUSTED')
            data['oauth'] += 1
            data['operations'][operation_id] = {'state': 'OAUTH_STARTED', 'epoch': data['revocation_epoch'], 'file': None}
            self._save(data)

    def reserve(self, operation_id, *, lane, bucket, file=None, rollback=False, create=False, refresh=False, cleanup=False):
        """Persist PREPARED and charge before a broker rechecks then dispatches."""
        with self.lock:
            require(type(operation_id) is str and 1 <= len(operation_id) <= 128, 'OPERATION_INVALID')
            require(lane in {'normal', 'safety'}, 'LANE_INVALID')
            caps = self.limits.buckets(lane)
            require(bucket in caps, 'BUCKET_INVALID')
            require(file in {None, 'A', 'B', 'C'}, 'FILE_INVALID')
            data = self._load()
            require((not data['revoked']) or cleanup, 'GRANT_REVOKED')
            require(not cleanup or (lane == 'safety' and bucket == 'revoke' and file is None and not refresh),
                    'CLEANUP_ONLY_DENIED')
            require(operation_id not in data['operations'], 'OPERATION_REPLAY')
            require(data['api_total'] < self.limits.api_total, 'API_BUDGET_EXHAUSTED')
            require(data['api_' + lane] < getattr(self.limits, 'api_' + lane), 'API_LANE_EXHAUSTED')
            require(data['buckets'][bucket] < caps[bucket], 'API_BUCKET_EXHAUSTED')
            require(not refresh or data['refresh'] < self.limits.refresh, 'REFRESH_BUDGET_EXHAUSTED')
            if file:
                require(not any(v.get('file') == file and v.get('state') == 'UNKNOWN'
                                for v in data['operations'].values()), 'FILE_UNKNOWN_RECONCILIATION_REQUIRED')
            if file:
                require(data['writes'][file] < getattr(self.limits, 'writes_' + file.lower()), 'WRITE_BUDGET_EXHAUSTED')
                if rollback: require(data['rollbacks'][file] < getattr(self.limits, 'rollbacks_' + file.lower()), 'ROLLBACK_BUDGET_EXHAUSTED')
                if not rollback: require(data['nonrollbacks'][file] < getattr(self.limits, 'nonrollbacks_' + file.lower()), 'NONROLLBACK_BUDGET_EXHAUSTED')
                if create: require(file == 'C' and data['creates_c'] < self.limits.creates_c, 'CREATE_BUDGET_EXHAUSTED')
            data['operations'][operation_id] = {'state': 'PREPARED', 'epoch': data['revocation_epoch'], 'file': file, 'cleanup': cleanup}
            self._save(data)
            data['api_total'] += 1; data['api_' + lane] += 1
            data['buckets'][bucket] += 1
            data['refresh'] += int(refresh)
            if file:
                data['writes'][file] += 1; data['rollbacks'][file] += int(rollback); data['nonrollbacks'][file] += int(not rollback); data['creates_c'] += int(create)
            self._save(data)
            return data['revocation_epoch']

    def dispatch(self, operation_id, epoch):
        """Only the future native broker may call this under its cross-process lock."""
        with self.lock:
            data = self._load()
            op = data['operations'].get(operation_id)
            require(type(op) is dict and op.get('state') == 'PREPARED'
                    and op.get('epoch') == data['revocation_epoch'] == epoch, 'DISPATCH_DENIED')
            require((not data['revoked']) or op.get('cleanup') is True, 'GRANT_REVOKED')
            op['state'] = 'DISPATCHED'; self._save(data)

    def complete(self, operation_id, outcome):
        with self.lock:
            data = self._load()
            require(outcome in {'VERIFIED', 'CONFLICT', 'FAILED', 'UNKNOWN', 'ROLLED_BACK'}, 'OUTCOME_INVALID')
            operation = data['operations'].get(operation_id)
            require(type(operation) is dict and operation.get('state') == 'DISPATCHED', 'OPERATION_STATE_INVALID')
            operation['state'] = outcome; self._save(data)

    def recover(self):
        with self.lock:
            data = self._load()
            require(not any(v.get('state') in {'PREPARED','DISPATCHED'} for v in data['operations'].values()),
                    'RECOVERY_REQUIRED')
            return data

    def revoke_local(self):
        with self.lock:
            data = self._load(); data['revocation_epoch'] += 1; data['revoked'] = True
            self._save(data); return data['revocation_epoch']
