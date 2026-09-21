"""Fixed HTTPS request grammar; no generic URL/method interface or redirects."""
import http.client
import ssl
import time
from urllib.parse import urlencode

from .contract import Denied, SCOPE, exact_json, require

META_FIELDS = 'id,mimeType,version,modifiedTime,trashed,isAppAuthorized'
ERROR_BYTES = 16_384
GOOGLE_ERRORS = frozenset({'invalid_client', 'invalid_grant', 'invalid_request', 'unauthorized_client',
                           'unsupported_grant_type', 'invalid_scope', 'access_denied', 'temporarily_unavailable'})
TOKEN_ERROR_HINTS = {'client_secret is missing.': 'CLIENT_SECRET_MISSING',
                     'Missing required parameter: client_secret': 'CLIENT_SECRET_MISSING'}
OPERATION_DIAGNOSTICS = {'identity': 'IDENTITY_READ', 'drive_before': 'DRIVE_BEFORE',
                         'docs_before': 'DOCS_BEFORE', 'body': 'DOCS_BODY', 'docs_after': 'DOCS_AFTER',
                         'drive_after': 'DRIVE_AFTER', 'exchange': 'TOKEN_EXCHANGE', 'revoke': 'TOKEN_REVOKE'}


class GoogleTransport:
    def __init__(self, binding):
        self.binding = binding
        self.counts = {'oauth_exchanges': 0, 'google_reads': 0, 'remote_revocations': 0}
        self.bytes = 0
        self.started = None
        self.last_provider_diagnostic = None

    def _provider_failure(self, operation, response):
        """Keep only fixed error metadata; response bytes never cross this boundary."""
        code, hint, size, chunks, started = 'UNKNOWN', 'UNCLASSIFIED', 0, [], time.monotonic()
        try:
            while True:
                if time.monotonic() - started >= 12:
                    chunks = []
                    break
                chunk = response.read1(4096)
                if not chunk:
                    break
                size += len(chunk)
                self.bytes += len(chunk)
                if self.bytes > 4_194_304:
                    chunks = []
                    break
                if size > ERROR_BYTES:
                    chunks = []
                    break
                chunks.append(chunk)
            if chunks and 'application/json' in (response.getheader('Content-Type') or ''):
                value = exact_json(b''.join(chunks))
                candidate = value.get('error') if type(value) is dict else None
                if candidate in GOOGLE_ERRORS:
                    code = candidate
                if operation == 'exchange' and type(value) is dict:
                    hint = TOKEN_ERROR_HINTS.get(value.get('error_description'), 'UNCLASSIFIED')
        except Exception:
            code = 'UNKNOWN'
        self.last_provider_diagnostic = {'operation': OPERATION_DIAGNOSTICS[operation],
                                         'http_status': response.status if type(response.status) is int else 0,
                                         'google_error': code, 'detail_hint': hint}

    def _exchange(self, operation, *, token=None, form=None):
        # Only private trusted broker code calls this method. No caller-supplied URLs.
        b = self.binding
        routes = {
            'identity': ('www.googleapis.com', '/drive/v3/about', {'fields': 'user(emailAddress,permissionId)'}),
            'drive_before': ('www.googleapis.com', '/drive/v3/files/' + b.file_id, {'fields': META_FIELDS}),
            'docs_before': ('docs.googleapis.com', '/v1/documents/' + b.file_id,
                         {'includeTabsContent': 'true'}),
            'body': ('docs.googleapis.com', '/v1/documents/' + b.file_id, {'includeTabsContent': 'true'}),
            'docs_after': ('docs.googleapis.com', '/v1/documents/' + b.file_id,
                         {'includeTabsContent': 'true'}),
            'drive_after': ('www.googleapis.com', '/drive/v3/files/' + b.file_id, {'fields': META_FIELDS}),
        }
        if operation in routes:
            require(token is not None and form is None, 'REQUEST_DENIED')
            # One identity proof plus M0/D0/B/D1/M1: six fixed GETs total.
            require(self.counts['google_reads'] < 6, 'REQUEST_BUDGET_EXHAUSTED')
            if self.started is None:
                self.started = time.monotonic()
            require(time.monotonic() - self.started < 45, 'READ_DEADLINE')
            host, path, query = routes[operation]
            path += '?' + urlencode(query)
            method, body = 'GET', None
            headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
            self.counts['google_reads'] += 1
        elif operation == 'exchange':
            require(self.counts['oauth_exchanges'] == 0 and token is None, 'REQUEST_DENIED')
            require(set(form) == {'client_id', 'code', 'code_verifier', 'grant_type', 'redirect_uri'}
                    and form['client_id'] == b.client_id and form['grant_type'] == 'authorization_code',
                    'REQUEST_DENIED')
            self.counts['oauth_exchanges'] += 1
            host, path, method = 'oauth2.googleapis.com', '/token', 'POST'
            body, headers = urlencode(form).encode(), {'Content-Type': 'application/x-www-form-urlencoded'}
        elif operation == 'revoke':
            require(self.counts['remote_revocations'] == 0 and token is not None and form is None,
                    'REQUEST_DENIED')
            self.counts['remote_revocations'] += 1
            host, path, method = 'oauth2.googleapis.com', '/revoke', 'POST'
            body, headers = urlencode({'token': token}).encode(), {'Content-Type': 'application/x-www-form-urlencoded'}
        else:
            raise Denied('REQUEST_DENIED')
        # HTTPSConnection does not consume proxy environment variables or follow redirects.
        conn = http.client.HTTPSConnection(host, timeout=8, context=ssl.create_default_context())
        started = time.monotonic()
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                self._provider_failure(operation, response)
                raise Denied({401: 'ACCESS_DENIED', 403: 'ACCESS_DENIED',
                              404: 'NOT_FOUND_OR_UNREADABLE', 429: 'RATE_LIMITED'}.get(response.status, 'PROVIDER_ERROR'))
            if operation == 'revoke':
                return {}
            chunks, size = [], 0
            while True:
                require(time.monotonic() - started < 12, 'RESPONSE_TIMEOUT')
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                self.bytes += len(chunk)
                require(size <= 2_097_152 and self.bytes <= 4_194_304, 'RESPONSE_LIMIT')
                chunks.append(chunk)
            require('application/json' in (response.getheader('Content-Type') or ''), 'RESPONSE_TYPE')
            return exact_json(b''.join(chunks))
        except Denied:
            raise
        except Exception:
            raise Denied('TRANSPORT_UNAVAILABLE') from None
        finally:
            conn.close()

    def exchange_code(self, code, verifier, redirect):
        return self._exchange('exchange', form={'client_id': self.binding.client_id,
            'code': code, 'code_verifier': verifier,
            'redirect_uri': redirect, 'grant_type': 'authorization_code'})

    def read(self, operation, token):
        require(operation in {'identity', 'drive_before', 'docs_before', 'body', 'docs_after', 'drive_after'}, 'REQUEST_DENIED')
        return self._exchange(operation, token=token)

    def revoke(self, token):
        return self._exchange('revoke', token=token)
