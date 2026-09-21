import json
import unittest
from unittest.mock import patch

from companion_mind.connector_alpha.contract import Denied
from companion_mind.connector_alpha.transport import GoogleTransport
from test_connector_alpha_transport import FakeTLS, Response, binding


class TokenErrorDiagnostics(unittest.TestCase):
    def setUp(self):
        FakeTLS.calls, FakeTLS.responses = [], []
        self.transport = GoogleTransport(binding())
        self.patch = patch('companion_mind.connector_alpha.transport.http.client.HTTPSConnection', FakeTLS)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def exchange(self):
        return self.transport.exchange_code('SYNTHETIC_ONLY_AUTHORIZATION_CODE', 'synthetic-verifier',
                                            'http://127.0.0.1:8765/callback')

    def test_allowlisted_token_error_never_leaks_body(self):
        secret = 'SYNTHETIC_SECRET_ERROR_DESCRIPTION'
        FakeTLS.responses = [Response(400, json.dumps({'error': 'invalid_grant', 'error_description': secret}).encode())]
        with self.assertRaisesRegex(Denied, 'PROVIDER_ERROR'): self.exchange()
        diagnostic = self.transport.last_provider_diagnostic
        self.assertEqual(diagnostic, {'operation': 'TOKEN_EXCHANGE', 'http_status': 400,
                                      'google_error': 'invalid_grant', 'detail_hint': 'UNCLASSIFIED'})
        self.assertNotIn(secret, json.dumps(diagnostic))

    def test_unknown_or_malformed_error_body_fails_closed(self):
        for body, kind in ((b'{"error":"not-allowlisted"}', 'application/json'),
                           (b'SYNTHETIC_SECRET_NOT_JSON', 'text/plain')):
            with self.subTest(kind=kind):
                self.transport = GoogleTransport(binding())
                FakeTLS.responses = [Response(500, body, kind)]
                with self.assertRaisesRegex(Denied, 'PROVIDER_ERROR'): self.exchange()
                self.assertEqual(self.transport.last_provider_diagnostic['google_error'], 'UNKNOWN')
                self.assertNotIn('SYNTHETIC_SECRET', json.dumps(self.transport.last_provider_diagnostic))

    def test_exact_allowlisted_description_is_classified_without_echo(self):
        FakeTLS.responses = [Response(400, json.dumps({'error': 'invalid_request',
                                                        'error_description': 'client_secret is missing.'}).encode())]
        with self.assertRaisesRegex(Denied, 'PROVIDER_ERROR'): self.exchange()
        diagnostic = self.transport.last_provider_diagnostic
        self.assertEqual(diagnostic['detail_hint'], 'CLIENT_SECRET_MISSING')
        self.assertNotIn('client_secret is missing.', json.dumps(diagnostic))

if __name__ == '__main__': unittest.main()
