"""Fixed, synthetic-only S3 purpose broker.

No HTTP client is present here.  Callers cannot supply a URL, callback, or raw
token; the broker is the only component that reads purpose-specific secrets.
"""
from dataclasses import dataclass
import hashlib

from .policy import Denied, require
from .stage import GenerationBinding, StageLifecycle


_HEX = set('0123456789abcdef')
_BUSINESS = {'docs.batchUpdate'}
_REFRESH = 'oauth.token.refresh'
_CLEANUP = 'oauth.token.revoke'


def _hash(value):
    require(type(value) is str and len(value) == 64 and set(value) <= _HEX, 'INTENT_INVALID')
    return value


@dataclass(frozen=True)
class RequestIntent:
    """A non-secret, fixed-endpoint request commitment; it deliberately has no URL."""
    method: str
    endpoint: str
    resource_id: str
    body_hash: str
    revision: str | None

    def __post_init__(self):
        require(type(self.method) is str and type(self.endpoint) is str
                and type(self.resource_id) is str and 1 <= len(self.resource_id) <= 180
                and (self.revision is None or (type(self.revision) is str and 1 <= len(self.revision) <= 1024)),
                'INTENT_INVALID')
        _hash(self.body_hash)
        require(self.endpoint in _BUSINESS | {_REFRESH, _CLEANUP}, 'ENDPOINT_DENIED')
        if self.endpoint == 'docs.batchUpdate':
            require(self.method == 'POST' and self.revision is not None, 'INTENT_INVALID')
        else:
            require(self.method == 'POST' and self.revision is None, 'INTENT_INVALID')

    @property
    def intent_hash(self):
        return hashlib.sha256((self.method + '\0' + self.endpoint + '\0' + self.resource_id + '\0'
                               + self.body_hash + '\0' + str(self.revision)).encode()).hexdigest()


@dataclass(frozen=True)
class DispatchReceipt:
    operation_id: str
    purpose: str
    endpoint: str
    resource_id: str
    intent_hash: str


class FixedSyntheticDispatcher:
    """Trusted fake transport.  It records hashes only and never exposes a token."""
    def __init__(self):
        self.records = []

    def dispatch(self, operation_id, purpose, intent, token):
        require(type(operation_id) is str and purpose in {'business', 'refresh', 'cleanup'}
                and isinstance(intent, RequestIntent) and type(token) is str and 20 <= len(token) <= 1024,
                'SYNTHETIC_DISPATCH_DENIED')
        intent_hash = hashlib.sha256((intent.method + '\x00' + intent.endpoint + '\x00'
                                      + intent.resource_id + '\x00' + intent.body_hash + '\x00'
                                      + str(intent.revision)).encode('utf-8')).hexdigest()
        receipt = DispatchReceipt(operation_id, purpose, intent.endpoint, intent.resource_id, intent_hash)
        self.records.append(receipt)
        return receipt


class PurposeBroker:
    """The only synthetic dispatch authority and stage-budget consumer for S3."""
    def __init__(self, lifecycle, binding, secrets, token_ref, dispatcher):
        require(isinstance(lifecycle, StageLifecycle) and isinstance(binding, GenerationBinding)
                and type(token_ref) is str and 1 <= len(token_ref) <= 220
                and type(dispatcher) is FixedSyntheticDispatcher
                and all(callable(getattr(secrets, name, None))
                        for name in ('business_token', 'refresh_token', 'cleanup_token')),
                'BROKER_CONFIGURATION_DENIED')
        self.lifecycle, self.binding = lifecycle, binding
        self.secrets, self.token_ref, self.dispatcher = secrets, token_ref, dispatcher

    def business(self, operation_id, intent, *, now, lane, bucket, file, rollback=False, create=False,
                 retest_package=None, intent_ref=None):
        require(isinstance(intent, RequestIntent) and intent.endpoint in _BUSINESS, 'ENDPOINT_DENIED')
        require(intent.resource_id == self._file_id(file), 'RESOURCE_DENIED')
        require(type(intent_ref) is str, 'INTENT_PIN_REQUIRED')
        with self.lifecycle.lock:
            self.lifecycle.reserve_request(self.binding, operation_id, now=now, lane=lane, bucket=bucket,
                                           file=file, rollback=rollback, create=create,
                                           retest_package=retest_package, intent_ref=intent_ref)
            self.lifecycle.assert_active(self.binding, now=now)
            return self.dispatcher.dispatch(operation_id, 'business', intent,
                                            self.secrets.business_token(self.token_ref))

    def pin_write(self, pin_id, intent, recovery_hash, *, now):
        require(isinstance(intent, RequestIntent) and intent.endpoint in _BUSINESS
                and type(recovery_hash) is str and len(recovery_hash) == 64, 'INTENT_PIN_DENIED')
        with self.lifecycle.lock:
            self.lifecycle.pin_write_intent(self.binding, pin_id, now=now, resource=intent.resource_id,
                                             revision=intent.revision, intent_hash=intent.intent_hash,
                                             recovery_hash=recovery_hash)
        return pin_id

    def refresh(self, operation_id, intent, *, now, lane='normal', bucket='continuity', retest_package=None):
        require(isinstance(intent, RequestIntent) and intent.endpoint == _REFRESH, 'ENDPOINT_DENIED')
        with self.lifecycle.lock:
            self.lifecycle.reserve_request(self.binding, operation_id, now=now, lane=lane, bucket=bucket,
                                           refresh=True, retest_package=retest_package)
            self.lifecycle.assert_active(self.binding, now=now)
            return self.dispatcher.dispatch(operation_id, 'refresh', intent,
                                            self.secrets.refresh_token(self.token_ref))

    def revoke_local(self):
        with self.lifecycle.lock:
            self.lifecycle.revoke_local(self.binding)

    def cleanup(self, operation_id, intent, *, now):
        require(isinstance(intent, RequestIntent) and intent.endpoint == _CLEANUP, 'ENDPOINT_DENIED')
        with self.lifecycle.lock:
            self.lifecycle.reserve_cleanup(self.binding, operation_id, now=now)
            self.lifecycle.assert_cleanup(self.binding, now=now)
            return self.dispatcher.dispatch(operation_id, 'cleanup', intent,
                                            self.secrets.cleanup_token(self.token_ref))

    def _file_id(self, file):
        require(file in {'A', 'B'}, 'RESOURCE_DENIED')
        return self.binding.a_id if file == 'A' else self.binding.b_id
