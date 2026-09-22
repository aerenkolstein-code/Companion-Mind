"""Independent black-box checks. Only fake HTTPS connections; no credentials/network."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs


from companion_mind.connector_alpha.contract import Binding, Denied, exact_json
from companion_mind.connector_alpha.transport import GoogleTransport


def binding():
    return Binding(installation='alpha-review', client_id='123-test.apps.googleusercontent.com',
        project_id='test-project', app_name='Independent synthetic review',
        subject_email='fixture@example.invalid', subject_permission_id='fixture-subject',
        owner='fixture-owner', universe='fixture-universe', purpose='single-doc-test',
        file_id='synthetic-allowed-file', windows_sid='S-1-5-21-100',
        expires_at='2099-01-01T00:00:00+00:00', dedicated_test_app=True,
        non_sensitive_test_file=True, revoke_project_grant_authorized=True)


class Response:
    def __init__(self, status=200, body=b'{}', kind='application/json'):
        self.status, self.body, self.kind = status, body, kind
    def read1(self, count):
        chunk, self.body = self.body[:count], self.body[count:]
        return chunk
    def getheader(self, key):
        return self.kind if key.lower() == 'content-type' else None


class FakeTLS:
    calls = []
    responses = []
    def __init__(self, host, **kwargs):
        self.host = host
    def request(self, method, path, body=None, headers=None):
        # Deliberately retain no credential/header values, even synthetic ones.
        self.calls.append((self.host, method, path))
    def getresponse(self):
        return self.responses.pop(0) if self.responses else Response()
    def close(self):
        pass


class TokenTLS(FakeTLS):
    forms = []
    def request(self, method, path, body=None, headers=None):
        super().request(method, path, body, headers)
        self.forms.append(parse_qs(body.decode('ascii'), strict_parsing=True))


class TransportBoundary(unittest.TestCase):
    def setUp(self):
        FakeTLS.calls, FakeTLS.responses = [], []
        self.t = GoogleTransport(binding())
        self.patch = patch('companion_mind.connector_alpha.transport.http.client.HTTPSConnection', FakeTLS)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_forbidden_actions_never_dispatch(self):
        for op in ['write', 'create', 'delete', 'move', 'share', 'list', 'search',
                   'documents.batchUpdate', 'https://example.invalid/', '../neighbor']:
            with self.subTest(op=op), self.assertRaises(Denied):
                self.t.read(op, 'SYNTHETIC_ONLY_TEST')
        self.assertEqual([], FakeTLS.calls)

    def test_exact_routes_and_single_resource(self):
        for op in ['identity', 'drive_before', 'docs_before', 'body']:
            self.t.read(op, 'SYNTHETIC_ONLY_TEST')
        self.assertEqual(4, len(FakeTLS.calls))
        for host, method, path in FakeTLS.calls:
            self.assertIn(host, {'www.googleapis.com', 'docs.googleapis.com'})
            self.assertEqual('GET', method)
            self.assertNotIn('SYNTHETIC_ONLY', path)
            self.assertTrue('/about?' in path or '/synthetic-allowed-file?' in path)

    def test_redirect_not_followed(self):
        FakeTLS.responses = [Response(302)]
        with self.assertRaises(Denied):
            self.t.read('drive_before', 'SYNTHETIC_ONLY_TEST')
        self.assertEqual(1, len(FakeTLS.calls))

    def test_provider_errors_do_not_export_response(self):
        for status in [401, 403, 404, 429, 500]:
            FakeTLS.responses = [Response(status, b'SYNTHETIC_ONLY_PRIVATE_PROVIDER_ERROR')]
            with self.subTest(status=status), self.assertRaises(Denied) as caught:
                self.t.read('drive_before', 'SYNTHETIC_ONLY_TEST')
            self.assertNotIn('SYNTHETIC_ONLY', str(caught.exception))

    def test_response_size_limit(self):
        FakeTLS.responses = [Response(body=b'x' * 2_097_153)]
        with self.assertRaises(Denied):
            self.t.read('body', 'SYNTHETIC_ONLY_TEST')

    def test_non_json_rejected(self):
        FakeTLS.responses = [Response(kind='text/html')]
        with self.assertRaises(Denied):
            self.t.read('body', 'SYNTHETIC_ONLY_TEST')

    def test_duplicate_keys_and_nonfinite_rejected(self):
        for raw in [b'{"id":1,"id":2}', b'{"value":NaN}', b'{"value":Infinity}']:
            with self.subTest(raw=raw), self.assertRaises(Denied):
                exact_json(raw)

    def test_requests_have_finite_budget(self):
        denied = False
        for _ in range(40):
            try:
                self.t.read('drive_before', 'SYNTHETIC_ONLY_TEST')
            except Denied:
                denied = True
                break
        self.assertTrue(denied)
        self.assertLess(len(FakeTLS.calls), 40)

    def test_token_exchange_is_the_only_secret_bearing_request(self):
        secret = 'SYNTHETIC_ONLY_DESKTOP_CLIENT_SECRET_0123456789'
        TokenTLS.calls, TokenTLS.responses, TokenTLS.forms = [], [Response()], []
        with patch('companion_mind.connector_alpha.transport.http.client.HTTPSConnection', TokenTLS):
            self.t.exchange_code('SYNTHETIC_ONLY_AUTHORIZATION_CODE', 'synthetic-verifier',
                                 'http://127.0.0.1:8765/callback', secret)
        self.assertEqual(TokenTLS.calls, [('oauth2.googleapis.com', 'POST', '/token')])
        self.assertEqual(TokenTLS.forms, [{'client_id': [self.t.binding.client_id], 'client_secret': [secret],
                                           'code': ['SYNTHETIC_ONLY_AUTHORIZATION_CODE'],
                                           'code_verifier': ['synthetic-verifier'],
                                           'grant_type': ['authorization_code'],
                                           'redirect_uri': ['http://127.0.0.1:8765/callback']}])
        self.assertNotIn(secret, repr(TokenTLS.calls))

    def test_missing_or_invalid_client_secret_never_dispatches(self):
        for secret in (None, '', 'too-short', 'contains space', 'x' * 2049):
            with self.subTest(secret=type(secret).__name__):
                self.t = GoogleTransport(binding())
                with self.assertRaises(Denied):
                    self.t.exchange_code('SYNTHETIC_ONLY_AUTHORIZATION_CODE', 'synthetic-verifier',
                                         'http://127.0.0.1:8765/callback', secret)
        self.assertEqual(FakeTLS.calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)

