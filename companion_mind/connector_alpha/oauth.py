"""Manual desktop OAuth PKCE and one-file Picker callback validation."""
from __future__ import annotations

import base64
import hashlib
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from urllib.parse import urlencode, parse_qs, urlparse

from .contract import Denied, MIME, SCOPE, require


def _token(n):
    return secrets.token_urlsafe(n)


class PkceFlow:
    def __init__(self, binding, port):
        require(type(port) is int and 1024 <= port <= 65535, 'LOOPBACK_PORT_INVALID')
        self.binding, self.port = binding, port
        self.state, self.verifier = _token(32), _token(64)
        self.used = False
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
        require(self.used is False, 'CALLBACK_REPLAYED')
        parsed = urlparse(callback_url)
        require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port == self.port
                and parsed.path == '/callback' and not parsed.fragment, 'CALLBACK_INVALID')
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        require(set(query) in ({'code', 'state', 'scope', 'picked_file_ids'},
                               {'error', 'state'}), 'CALLBACK_INVALID')
        require(query.get('state') == [self.state], 'STATE_MISMATCH')
        require('error' not in query, 'CONSENT_CANCELLED')
        require(query.get('scope') == [SCOPE], 'SCOPE_MISMATCH')
        require(query.get('picked_file_ids') == [self.binding.file_id], 'PICKER_FILE_MISMATCH')
        require(len(query.get('code', ())) == 1 and len(query.get('scope', ())) == 1
                and len(query.get('picked_file_ids', ())) == 1, 'CALLBACK_INVALID')
        code = query['code'][0]
        require(20 <= len(code) <= 2048 and all(ord(c) >= 33 for c in code), 'AUTHORIZATION_CODE_INVALID')
        self.used = True
        return code

    def bind_listener(self):
        """Bind exactly one loopback listener before opening the browser."""
        flow, received, done = self, {}, threading.Event()
        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_): return
            def do_GET(self):
                try:
                    self.connection.settimeout(2)
                    require(self.headers.get_all('Host') == ['127.0.0.1:%d' % flow.port]
                            and self.headers.get('Content-Length') in (None, '0')
                            and self.headers.get('Transfer-Encoding') is None
                            and len(self.path.encode('ascii')) <= 4096, 'CALLBACK_INVALID')
                    received['code'] = flow.accept_callback('http://127.0.0.1:%d%s' % (flow.port, self.path))
                    self.send_response(204)
                except Exception as exc:
                    received['error'] = str(exc) if type(exc) is Denied else 'CALLBACK_INVALID'
                    self.send_response(400)
                self.end_headers()
        server = HTTPServer(('127.0.0.1', self.port), Callback); server.timeout = .2
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
