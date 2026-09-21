"""Manual desktop OAuth PKCE and one-file Picker callback validation."""
from __future__ import annotations

import base64
import hashlib
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from urllib.parse import urlencode, parse_qs, urlparse

from .contract import Denied, MIME, SCOPE, require

GOOGLE_ISSUER = 'https://accounts.google.com'
_SUCCESS = ('code', 'state', 'scope', 'picked_file_ids')
_DIAGNOSTIC = (*_SUCCESS, 'error', 'iss')


def _token(n):
    return secrets.token_urlsafe(n)


class PkceFlow:
    def __init__(self, binding, port):
        require(type(port) is int and 1024 <= port <= 65535, 'LOOPBACK_PORT_INVALID')
        self.binding, self.port = binding, port
        self.state, self.verifier = _token(32), _token(64)
        self.used = False
        self.callback_diagnostic = None
        self.redirect_uri = 'http://127.0.0.1:%d/callback' % port

    @property
    def authorization_url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': self.binding.client_id, 'redirect_uri': self.redirect_uri,
                  'response_type': 'code', 'scope': SCOPE, 'state': self.state,
                  'code_challenge': challenge, 'code_challenge_method': 'S256',
                  'access_type': 'online', 'prompt': 'consent', 'trigger_onepick': 'true',
                  'file_ids': self.binding.file_id, 'mimetypes': MIME}
        return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(params)

    def accept_callback(self, callback_url):
        self.callback_diagnostic = self.diagnose_callback(callback_url)
        require(self.used is False, 'CALLBACK_REPLAYED')
        parsed = urlparse(callback_url)
        require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port == self.port
                and parsed.path == '/callback' and not parsed.fragment, 'CALLBACK_INVALID')
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        require(all(len(values) == 1 for values in query.values()), 'CALLBACK_DUPLICATE_PARAMETER')
        has_error = 'error' in query
        has_success = any(name in query for name in ('code', 'scope', 'picked_file_ids'))
        require(not (has_error and has_success), 'CALLBACK_MIXED_RESPONSE')
        require(query.get('state') == [self.state], 'STATE_MISMATCH')
        if 'iss' in query:
            require(query['iss'] == [GOOGLE_ISSUER], 'ISSUER_MISMATCH')
        if has_error:
            require(query.get('error', [''])[0] != '', 'CALLBACK_INVALID')
            raise Denied('CONSENT_CANCELLED')
        require(all(name in query and query[name][0] != '' for name in _SUCCESS), 'CALLBACK_INVALID')
        require(query.get('scope') == [SCOPE], 'SCOPE_MISMATCH')
        require(query.get('picked_file_ids') == [self.binding.file_id], 'PICKER_FILE_MISMATCH')
        code = query['code'][0]
        require(20 <= len(code) <= 2048 and all(ord(c) >= 33 for c in code), 'AUTHORIZATION_CODE_INVALID')
        self.used = True
        self.callback_diagnostic = {**self.callback_diagnostic, 'stage': 'ACCEPTED'}
        return code

    def diagnose_callback(self, callback_url):
        """Return only a bounded, non-secret callback failure projection."""
        names = _DIAGNOSTIC
        empty = {'stage': 'URL_PARSE', 'known_present': {name: False for name in names},
                 'known_duplicate': {name: False for name in names}, 'unknown_field_count': 0}
        try:
            parsed = urlparse(callback_url)
            if not (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port == self.port):
                return {**empty, 'stage': 'HTTP_ORIGIN'}
            if parsed.path != '/callback' or parsed.fragment:
                return {**empty, 'stage': 'HTTP_PATH'}
            pairs = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        except Exception:
            return empty
        present = {name: name in pairs for name in names}
        duplicate = {name: len(pairs.get(name, ())) > 1 for name in names}
        unknown = sum(1 for name in pairs if name not in names)
        result = {'stage': 'QUERY_SHAPE', 'known_present': present,
                  'known_duplicate': duplicate, 'unknown_field_count': unknown}
        if self.used:
            return {**result, 'stage': 'REPLAY'}
        if any(duplicate.values()) or any(len(values) > 1 for values in pairs.values()):
            return {**result, 'stage': 'QUERY_DUPLICATE'}
        has_error = 'error' in pairs
        has_success = any(name in pairs for name in ('code', 'scope', 'picked_file_ids'))
        if has_error and has_success:
            return {**result, 'stage': 'RESPONSE_MIXED'}
        if pairs.get('state') != [self.state]:
            return {**result, 'stage': 'STATE'}
        if 'iss' in pairs and pairs['iss'] != [GOOGLE_ISSUER]:
            return {**result, 'stage': 'ISSUER'}
        if has_error:
            return {**result, 'stage': 'CONSENT' if pairs.get('error') != [''] else 'ERROR_CORE'}
        if not all(name in pairs and pairs[name] != [''] for name in _SUCCESS):
            return {**result, 'stage': 'SUCCESS_CORE'}
        if pairs.get('scope') != [SCOPE]:
            return {**result, 'stage': 'SCOPE'}
        if pairs.get('picked_file_ids') != [self.binding.file_id]:
            return {**result, 'stage': 'PICKER_FILE'}
        code = pairs.get('code', [''])[0]
        if not (20 <= len(code) <= 2048 and all(ord(c) >= 33 for c in code)):
            return {**result, 'stage': 'CODE'}
        return {**result, 'stage': 'ACCEPTED'}

    def bind_listener(self):
        """Bind exactly one loopback listener before opening the browser."""
        flow, received, done = self, {}, threading.Event()
        class QuietServer(HTTPServer):
            def handle_error(self, *_): return
        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_): return
            def setup(self):
                self.request.settimeout(2)
                super().setup()
            def do_GET(self):
                try:
                    require(self.headers.get_all('Host') == ['127.0.0.1:%d' % flow.port]
                            and self.headers.get('Content-Length') in (None, '0')
                            and self.headers.get('Transfer-Encoding') is None
                            and len(self.path.encode('ascii')) <= 4096, 'CALLBACK_INVALID')
                    # Browsers commonly probe /favicon.ico. It is not an OAuth
                    # result and must not consume the one callback opportunity.
                    if urlparse(self.path).path != '/callback':
                        self.send_response(404)
                        self.end_headers()
                        return
                    received['code'] = flow.accept_callback('http://127.0.0.1:%d%s' % (flow.port, self.path))
                    self.send_response(204)
                except Exception as exc:
                    if flow.callback_diagnostic is None:
                        flow.callback_diagnostic = {'stage': 'HTTP_HEADER',
                                                    'known_present': {'code': False, 'state': False, 'scope': False,
                                                                      'picked_file_ids': False, 'error': False, 'iss': False},
                                                    'known_duplicate': {'code': False, 'state': False, 'scope': False,
                                                                        'picked_file_ids': False, 'error': False, 'iss': False},
                                                    'unknown_field_count': 0}
                    received['error'] = str(exc) if type(exc) is Denied else 'CALLBACK_INVALID'
                    self.send_response(400)
                self.end_headers()
        server = QuietServer(('127.0.0.1', self.port), Callback); server.timeout = .2
        def serve():
            while not done.is_set() and not received: server.handle_request()
        thread = threading.Thread(target=serve, daemon=True); thread.start()
        return server, thread, done, received

    @staticmethod
    def close_listener(listener):
        server, thread, done, _ = listener
        done.set(); server.server_close(); thread.join(2)

    def await_callback(self, listener, timeout=300):
        require(type(timeout) is int and 1 <= timeout <= 600, 'CALLBACK_TIMEOUT_INVALID')
        server, thread, done, received = listener
        thread.join(timeout); done.set(); server.server_close()
        require(not thread.is_alive() and 'code' in received, received.get('error', 'CALLBACK_TIMEOUT'))
        return received['code']
