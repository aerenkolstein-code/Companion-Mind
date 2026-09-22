"""Fixed S3 request grammar and mock-only transport; never opens a socket."""
from dataclasses import dataclass
import hashlib
from .policy import require

_ACTIONS = {
    'token.exchange': 'POST', 'token.refresh': 'POST', 'token.revoke': 'POST',
    'about.user.get': 'GET', 'drive.metadata.get': 'GET', 'docs.get': 'GET',
    'docs.batchUpdate': 'POST', 'drive.c.create': 'POST',
}


@dataclass(frozen=True)
class FixedRequest:
    action: str
    method: str
    resource: str
    body_hash: str
    revision: str | None = None

    def __post_init__(self):
        require(type(self.action) is str and self.action in _ACTIONS and self.method == _ACTIONS[self.action]
                and type(self.resource) is str and 1 <= len(self.resource) <= 180
                and type(self.body_hash) is str and len(self.body_hash) == 64
                and all(c in '0123456789abcdef' for c in self.body_hash)
                and (self.revision is None or (type(self.revision) is str and self.revision)),
                'FIXED_REQUEST_DENIED')
        require((self.action == 'docs.batchUpdate') == (self.revision is not None), 'FIXED_REQUEST_DENIED')

    @property
    def intent_hash(self):
        return hashlib.sha256(('%s\0%s\0%s\0%s\0%s' %
            (self.action, self.method, self.resource, self.body_hash, self.revision)).encode()).hexdigest()


class MockFixedTransport:
    """Deliberately type-gated fake transport with no URL, retry, or redirect API."""
    def __init__(self, responses=None):
        self.responses, self.records = dict(responses or {}), []

    def dispatch(self, request):
        require(type(request) is FixedRequest, 'FIXED_TRANSPORT_DENIED')
        self.records.append({'action': request.action, 'intent_hash': request.intent_hash})
        return dict(self.responses.get(request.action, {'status': 'MOCK_OK'}))
