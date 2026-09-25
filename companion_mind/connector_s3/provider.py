"""Fixed Google HTTPS grammar. Only the qualified, precharged broker may call it.

No generic URL, redirects, proxy-environment consumption, retries or logging.
All returned content is private local data, not a model/tool-output payload.
"""
import http.client
import json
import re
import ssl
import time
from urllib.parse import urlencode, urlparse

from .docs_plan import EditPlan, Snapshot, canonical, checked_text, parse_document
from .native import _json
from .oauth import SCOPE
from .policy import Denied, require
from .stage import GenerationBinding

DOC = 'application/vnd.google-apps.document'
FOLDER = 'application/vnd.google-apps.folder'
META_FIELDS = 'id,mimeType,parents,trashed,driveId,shortcutDetails,capabilities(canEdit,canAddChildren),isAppAuthorized,version,modifiedTime'


class ProviderFailure(Denied):
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.http_status = http_status


def _identifier(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,180}', value), 'RESOURCE_INVALID')
    return value


def _secret(value):
    require(type(value) is str and 20 <= len(value) <= 2048
            and all(33 <= ord(c) <= 126 for c in value), 'SECRET_FORMAT_INVALID')
    return value


def validate_write(plan, allowed_ids):
    require(type(plan) is EditPlan and type(plan.before) is Snapshot
            and plan.before.document_id in allowed_ids, 'WRITE_PLAN_DENIED')
    before = plan.before
    require(parse_document(_json(before.raw), before.document_id) == before, 'WRITE_PLAN_DENIED')
    data = _json(plan.request_bytes)
    require(type(data) is dict and set(data) == {'writeControl', 'requests'}
            and data['writeControl'] == {'requiredRevisionId': before.revision}
            and type(data['requests']) is list and 1 <= len(data['requests']) <= 2, 'WRITE_PLAN_DENIED')
    text = before.text.encode('utf-16-le')
    delete_index = None
    for index, request in enumerate(data['requests']):
        require(type(request) is dict and len(request) == 1, 'WRITE_PLAN_DENIED')
        if 'deleteContentRange' in request:
            value = request['deleteContentRange']
            require(index == 0 and type(value) is dict and set(value) == {'range'}, 'WRITE_PLAN_DENIED')
            span = value['range']
            require(type(span) is dict and set(span) == {'tabId', 'startIndex', 'endIndex'}
                    and span['tabId'] == before.tab_id and type(span['startIndex']) is int
                    and type(span['endIndex']) is int and 1 <= span['startIndex'] < span['endIndex'] <= len(text)//2,
                    'WRITE_PLAN_DENIED')
            delete_index = span['startIndex']
            lo, hi = 2*(span['startIndex']-1), 2*(span['endIndex']-1)
            text = text[:lo] + text[hi:]
        elif 'insertText' in request:
            value = request['insertText']
            require(type(value) is dict and set(value) == {'location', 'text'}
                    and type(value['text']) is str and bool(value['text']), 'WRITE_PLAN_DENIED')
            location = value['location']
            require(type(location) is dict and set(location) == {'tabId', 'index'}
                    and location['tabId'] == before.tab_id and type(location['index']) is int
                    and 1 <= location['index'] <= len(text)//2
                    and (index == 0 or location['index'] == delete_index)
                    and index == len(data['requests'])-1, 'WRITE_PLAN_DENIED')
            lo = 2*(location['index']-1)
            checked_text(value['text'])
            text = text[:lo] + value['text'].encode('utf-16-le') + text[lo:]
        else:
            raise Denied('WRITE_PLAN_DENIED')
    try:
        actual = text.decode('utf-16-le')
        require(actual == plan.after_text and actual != before.text and actual.endswith('\n')
                and len(actual.encode('utf-8')) <= 32768, 'WRITE_PLAN_DENIED')
    except UnicodeError:
        raise Denied('WRITE_PLAN_DENIED') from None
    require(canonical(data) == plan.request_bytes, 'WRITE_PLAN_DENIED')
    return plan.request_bytes


class GoogleProvider:
    def __init__(self, binding, c_id=None):
        require(type(binding) is GenerationBinding, 'BINDING_INVALID')
        for value in (binding.folder_id, binding.a_id, binding.b_id):
            _identifier(value)
        require(c_id is None or (_identifier(c_id) not in {binding.folder_id, binding.a_id, binding.b_id}), 'RESOURCE_INVALID')
        self.binding, self.c_id = binding, c_id
        self._pending_c = None
        self._create_started = c_id is not None
        self.started, self.response_bytes = None, 0

    def _document_ids(self):
        return {self.binding.a_id, self.binding.b_id} | ({self.c_id} if self.c_id else set())

    def _send(self, action, *, token=None, resource_id=None, plan=None, form_data=None):
        require(type(action) is str and action in {'identity', 'metadata', 'document', 'write', 'create', 'exchange', 'refresh', 'revoke'},
                'ENDPOINT_DENIED')
        body, form, revoke, authorization_token = None, False, action == 'revoke', token
        if action == 'identity':
            host, path, method = 'www.googleapis.com', '/drive/v3/about?' + urlencode({'fields': 'user(emailAddress,permissionId)'}), 'GET'
        elif action == 'metadata':
            require(resource_id in self._document_ids() | {self.binding.folder_id, self._pending_c}, 'RESOURCE_DENIED')
            _identifier(resource_id)
            host, path, method = 'www.googleapis.com', '/drive/v3/files/' + resource_id + '?' + urlencode({'fields': META_FIELDS}), 'GET'
        elif action == 'document':
            require(resource_id in self._document_ids(), 'RESOURCE_DENIED')
            query = urlencode({'includeTabsContent': 'true', 'suggestionsViewMode': 'SUGGESTIONS_INLINE'})
            host, path, method = 'docs.googleapis.com', '/v1/documents/' + resource_id + '?' + query, 'GET'
        elif action == 'write':
            body = validate_write(plan, self._document_ids())
            host, path, method = 'docs.googleapis.com', '/v1/documents/' + plan.before.document_id + ':batchUpdate', 'POST'
        elif action == 'create':
            require(not self._create_started, 'CREATE_ALREADY_ATTEMPTED')
            self._create_started = True
            body = canonical({'name': 'A1 S3 Synthetic Result', 'mimeType': DOC, 'parents': [self.binding.folder_id]})
            host, path, method = 'www.googleapis.com', '/drive/v3/files?' + urlencode({'fields': META_FIELDS}), 'POST'
        elif action in {'exchange', 'refresh'}:
            self._validate_form(action, form_data)
            authorization_token, form = None, True
            body = urlencode(form_data).encode('ascii')
            host, path, method = 'oauth2.googleapis.com', '/token', 'POST'
        else:
            authorization_token, form = None, True
            body = urlencode({'token': _secret(token)}).encode('ascii')
            host, path, method = 'oauth2.googleapis.com', '/revoke', 'POST'
        if self.started is None:
            self.started = time.monotonic()
        require(time.monotonic() - self.started < 180, 'TRANSACTION_DEADLINE')
        headers = {'Accept': 'application/json'}
        if authorization_token is not None:
            headers['Authorization'] = 'Bearer ' + _secret(authorization_token)
        require(action in {'exchange', 'refresh', 'revoke'} or authorization_token is not None, 'SECRET_FORMAT_INVALID')
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        connection = http.client.HTTPSConnection(host, timeout=8, context=ssl.create_default_context())
        started = time.monotonic()
        try:
            connection.connect()
            require(time.monotonic() - started < 8, 'CONNECT_DEADLINE')
            remaining = min(30 - (time.monotonic()-started), 180 - (time.monotonic()-self.started))
            require(remaining > 0 and connection.sock is not None, 'REQUEST_DEADLINE')
            response_socket = connection.sock
            response_socket.settimeout(remaining)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status not in ({200, 204} if revoke else {200, 201}):
                code = {400: 'REQUEST_REJECTED', 401: 'ACCESS_DENIED', 403: 'ACCESS_DENIED',
                        404: 'NOT_FOUND_OR_UNREADABLE', 409: 'PRECONDITION_FAILED',
                        412: 'PRECONDITION_FAILED', 429: 'RATE_LIMITED'}.get(response.status, 'PROVIDER_ERROR')
                raise ProviderFailure(code, response.status)
            if revoke:
                return {'revoked': True}
            require('application/json' in (response.getheader('Content-Type') or '').lower(), 'RESPONSE_TYPE')
            chunks, count = [], 0
            while True:
                remaining = min(30 - (time.monotonic()-started), 180 - (time.monotonic()-self.started))
                require(remaining > 0, 'REQUEST_DEADLINE')
                response_socket.settimeout(remaining)
                chunk = response.read1(65536)
                if not chunk:
                    break
                count += len(chunk)
                self.response_bytes += len(chunk)
                require(count <= 1048576 and self.response_bytes <= 8388608, 'RESPONSE_LIMIT')
                chunks.append(chunk)
            value = _json(b''.join(chunks))
            require(type(value) is dict, 'RESPONSE_INVALID')
            return value
        except Denied:
            raise
        except Exception:
            raise ProviderFailure('TRANSPORT_UNKNOWN') from None
        finally:
            connection.close()

    def identity(self, token):
        data = self._send('identity', token=token)
        user = data.get('user')
        require(type(user) is dict and user.get('emailAddress') == self.binding.account
                and user.get('permissionId') == self.binding.subject, 'IDENTITY_MISMATCH')
        return {'verified': True}

    def metadata(self, token, resource_id):
        require(resource_id in self._document_ids() | {self.binding.folder_id, self._pending_c}, 'RESOURCE_DENIED')
        _identifier(resource_id)
        data = self._send('metadata', token=token, resource_id=resource_id)
        self.verify_metadata(data, resource_id)
        return data

    def verify_metadata(self, data, resource_id):
        is_folder = resource_id == self.binding.folder_id
        require(type(data) is dict and data.get('id') == resource_id and data.get('mimeType') == (FOLDER if is_folder else DOC)
                and data.get('trashed') is False and not data.get('driveId') and not data.get('shortcutDetails')
                and data.get('isAppAuthorized') is True, 'RESOURCE_CAPABILITY_DENIED')
        capabilities = data.get('capabilities')
        require(type(capabilities) is dict and capabilities.get('canEdit') is True, 'RESOURCE_CAPABILITY_DENIED')
        if is_folder:
            require(capabilities.get('canAddChildren') is True, 'RESOURCE_CAPABILITY_DENIED')
        else:
            require(data.get('parents') == [self.binding.folder_id], 'RESOURCE_PARENT_DENIED')

    def document(self, token, resource_id):
        require(resource_id in self._document_ids(), 'RESOURCE_DENIED')
        data = self._send('document', token=token, resource_id=resource_id)
        return parse_document(data, resource_id)

    def write_document(self, token, plan):
        return self._send('write', token=token, plan=plan)

    def create_c(self, token):
        data = self._send('create', token=token)
        candidate = _identifier(data.get('id'))
        require(candidate not in {self.binding.folder_id, self.binding.a_id, self.binding.b_id}, 'RESOURCE_DENIED')
        self.verify_metadata(data, candidate)
        self._pending_c = candidate
        return data

    def admit_created_c(self, readback):
        require(self.c_id is None and self._pending_c is not None, 'CREATE_READBACK_REQUIRED')
        self.verify_metadata(readback, self._pending_c)
        self.c_id, self._pending_c = self._pending_c, None
        return self.c_id

    def exchange(self, code, verifier, redirect_uri, client_secret):
        form = {'client_id': self.binding.client_id, 'client_secret': _secret(client_secret),
                'code': _secret(code), 'code_verifier': verifier, 'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code'}
        return self._token_response(self._send('exchange', form_data=form), require_refresh=True)

    def refresh(self, refresh_token, client_secret):
        form = {'client_id': self.binding.client_id, 'client_secret': _secret(client_secret),
                'refresh_token': _secret(refresh_token), 'grant_type': 'refresh_token'}
        return self._token_response(self._send('refresh', form_data=form), require_refresh=False)

    def _validate_form(self, action, form):
        extra = {'code', 'code_verifier', 'redirect_uri'} if action == 'exchange' else {'refresh_token'}
        require(type(form) is dict and set(form) == {'client_id', 'client_secret', 'grant_type'} | extra
                and form['client_id'] == self.binding.client_id
                and form['grant_type'] == ('authorization_code' if action == 'exchange' else 'refresh_token'), 'TOKEN_FORM_DENIED')
        _secret(form['client_secret'])
        if action == 'refresh':
            _secret(form['refresh_token'])
            return
        _secret(form['code'])
        require(type(form['code_verifier']) is str and re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', form['code_verifier']), 'PKCE_INVALID')
        try:
            parsed = urlparse(form['redirect_uri'])
            require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.path == '/callback'
                    and parsed.port is not None and 1024 <= parsed.port <= 65535
                    and parsed.netloc == '127.0.0.1:%d' % parsed.port
                    and not parsed.query and not parsed.fragment, 'REDIRECT_DENIED')
        except (TypeError, ValueError):
            raise Denied('REDIRECT_DENIED') from None

    @staticmethod
    def _token_response(data, require_refresh):
        require(data.get('token_type') == 'Bearer' and data.get('scope') == SCOPE
                and type(data.get('expires_in')) is int and 0 < data['expires_in'] <= 3600, 'TOKEN_RESPONSE_INVALID')
        _secret(data.get('access_token'))
        if require_refresh or 'refresh_token' in data:
            _secret(data.get('refresh_token'))
        return data

    def revoke(self, token):
        return self._send('revoke', token=token)
