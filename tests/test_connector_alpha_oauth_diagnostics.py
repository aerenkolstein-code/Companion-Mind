import json
import http.client
import socket
import unittest
from urllib.parse import urlencode

from companion_mind.connector_alpha.contract import SCOPE
from companion_mind.connector_alpha.oauth import PkceFlow
from test_connector_alpha_transport import binding


class CallbackDiagnostics(unittest.TestCase):
    def flow(self): return PkceFlow(binding(), 18765)

    def test_secret_values_and_unknown_names_never_egress(self):
        flow = self.flow()
        raw = flow.redirect_uri + '?' + urlencode({'state': 'wrong-secret-value', 'code': 'VERY_SECRET_CODE_VALUE',
                                                    'scope': SCOPE, 'picked_file_ids': flow.binding.file_id,
                                                    'unrecognized_secret_parameter': 'hidden'})
        diagnostic = flow.diagnose_callback(raw)
        rendered = json.dumps(diagnostic)
        self.assertEqual((diagnostic['stage'], diagnostic['unknown_field_count']), ('STATE', 1))
        self.assertNotIn('VERY_SECRET_CODE_VALUE', rendered)
        self.assertNotIn('wrong-secret-value', rendered)
        self.assertNotIn('unrecognized_secret_parameter', rendered)

    def test_duplicate_and_illegal_callbacks_have_bounded_stages(self):
        flow = self.flow()
        duplicate = flow.redirect_uri + '?state=' + flow.state + '&state=other&code=' + 'x' * 24
        result = flow.diagnose_callback(duplicate)
        self.assertEqual(result['stage'], 'QUERY_DUPLICATE')
        self.assertTrue(result['known_duplicate']['state'])
        self.assertEqual(flow.diagnose_callback('https://evil.invalid/callback')['stage'], 'HTTP_ORIGIN')
        self.assertEqual(flow.diagnose_callback('http://127.0.0.1:18765/not-callback')['stage'], 'HTTP_PATH')

    def test_single_extensions_are_ignored_but_cannot_bypass_core(self):
        flow = self.flow()
        good = flow.redirect_uri + '?' + urlencode({'state': flow.state, 'scope': SCOPE,
            'picked_file_ids': flow.binding.file_id, 'code': 'SYNTHETIC_ONLY_AUTHORIZATION_CODE',
            'opaque_extension': 'unrecorded-value'})
        self.assertEqual(flow.diagnose_callback(good)['stage'], 'ACCEPTED')
        self.assertTrue(flow.accept_callback(good).startswith('SYNTHETIC_ONLY_'))
        flow = self.flow()
        mixed = flow.redirect_uri + '?' + urlencode({'state': flow.state, 'scope': SCOPE,
            'picked_file_ids': flow.binding.file_id, 'code': 'SYNTHETIC_ONLY_AUTHORIZATION_CODE',
            'error': 'access_denied', 'opaque_extension': 'unrecorded-value'})
        self.assertEqual(flow.diagnose_callback(mixed)['stage'], 'RESPONSE_MIXED')
        with self.assertRaisesRegex(Exception, 'CALLBACK_MIXED_RESPONSE'): flow.accept_callback(mixed)

    def test_issuer_and_unknown_duplicates_fail_closed_without_leakage(self):
        flow = self.flow()
        base = {'state': flow.state, 'scope': SCOPE, 'picked_file_ids': flow.binding.file_id,
                'code': 'SYNTHETIC_ONLY_AUTHORIZATION_CODE'}
        wrong_issuer = flow.redirect_uri + '?' + urlencode({**base, 'iss': 'https://evil.invalid'})
        self.assertEqual(flow.diagnose_callback(wrong_issuer)['stage'], 'ISSUER')
        with self.assertRaisesRegex(Exception, 'ISSUER_MISMATCH'): flow.accept_callback(wrong_issuer)
        duplicate_unknown = flow.redirect_uri + '?' + urlencode(base) + '&extension=value&extension=other'
        result = flow.diagnose_callback(duplicate_unknown)
        self.assertEqual((result['stage'], result['unknown_field_count']), ('QUERY_DUPLICATE', 1))
        self.assertNotIn('extension', json.dumps(result))
        with self.assertRaisesRegex(Exception, 'CALLBACK_DUPLICATE_PARAMETER'): flow.accept_callback(duplicate_unknown)

    def test_core_fields_remain_strict_with_extensions(self):
        base = {'state': None, 'scope': SCOPE, 'picked_file_ids': None,
                'code': 'SYNTHETIC_ONLY_AUTHORIZATION_CODE', 'opaque_extension': 'secret-extension-value'}
        for field in ('code', 'state', 'scope', 'picked_file_ids'):
            for mode in ('missing', 'empty'):
                with self.subTest(field=field, mode=mode):
                    flow = self.flow()
                    candidate = dict(base, state=flow.state, picked_file_ids=flow.binding.file_id)
                    if mode == 'missing': del candidate[field]
                    else: candidate[field] = ''
                    url = flow.redirect_uri + '?' + urlencode(candidate)
                    with self.assertRaises(Exception): flow.accept_callback(url)
                    self.assertNotEqual(flow.callback_diagnostic['stage'], 'ACCEPTED')
        for field, value in (('state', 'wrong-state'), ('scope', 'wrong-scope'), ('picked_file_ids', 'wrong-file')):
            with self.subTest(field=field):
                flow = self.flow()
                candidate = dict(base, state=flow.state, picked_file_ids=flow.binding.file_id)
                candidate[field] = value
                with self.assertRaises(Exception): flow.accept_callback(flow.redirect_uri + '?' + urlencode(candidate))
        flow = self.flow()
        duplicate = flow.redirect_uri + '?' + urlencode({**base, 'state': flow.state, 'picked_file_ids': flow.binding.file_id})
        duplicate += '&scope=' + SCOPE
        with self.assertRaisesRegex(Exception, 'CALLBACK_DUPLICATE_PARAMETER'): flow.accept_callback(duplicate)

    def test_extension_success_projection_keeps_count_without_values(self):
        flow = self.flow()
        url = flow.redirect_uri + '?' + urlencode({'state': flow.state, 'scope': SCOPE,
            'picked_file_ids': flow.binding.file_id, 'code': 'SYNTHETIC_ONLY_AUTHORIZATION_CODE',
            'opaque_extension': 'secret-extension-value'})
        flow.accept_callback(url)
        diagnostic = json.dumps(flow.callback_diagnostic)
        self.assertEqual(flow.callback_diagnostic['unknown_field_count'], 1)
        for forbidden in ('opaque_extension', 'secret-extension-value', 'SYNTHETIC_ONLY_AUTHORIZATION_CODE', flow.state):
            self.assertNotIn(forbidden, diagnostic)

    def test_empty_error_and_error_issuer_are_not_valid_consent(self):
        flow = self.flow()
        empty = flow.redirect_uri + '?' + urlencode({'state': flow.state, 'error': ''})
        self.assertEqual(flow.diagnose_callback(empty)['stage'], 'ERROR_CORE')
        with self.assertRaisesRegex(Exception, 'CALLBACK_INVALID'): flow.accept_callback(empty)
        flow = self.flow()
        bad_issuer = flow.redirect_uri + '?' + urlencode({'state': flow.state, 'error': 'access_denied',
                                                            'iss': 'https://evil.invalid'})
        self.assertEqual(flow.diagnose_callback(bad_issuer)['stage'], 'ISSUER')
        with self.assertRaisesRegex(Exception, 'ISSUER_MISMATCH'): flow.accept_callback(bad_issuer)

    def test_unrelated_favicon_does_not_consume_callback(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        flow, listener = PkceFlow(binding(), port), None
        try:
            listener = flow.bind_listener()
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            conn.request('GET', '/favicon.ico')
            self.assertEqual(conn.getresponse().status, 404)
            conn.close()
            with self.assertRaisesRegex(Exception, 'CALLBACK_TIMEOUT'):
                flow.await_callback(listener, 1)
        finally:
            if listener is not None: flow.close_listener(listener)

    def test_favicon_then_exact_callback_succeeds(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        flow, listener = PkceFlow(binding(), port), None
        try:
            listener = flow.bind_listener()
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            conn.request('GET', '/favicon.ico'); self.assertEqual(conn.getresponse().status, 404)
            path = '/callback?' + urlencode({'state': flow.state, 'scope': SCOPE,
                'picked_file_ids': flow.binding.file_id, 'code': 'SYNTHETIC_ONLY_AUTHORIZATION_CODE'})
            conn.request('GET', path); self.assertEqual(conn.getresponse().status, 204)
            self.assertTrue(flow.await_callback(listener, 3).startswith('SYNTHETIC_ONLY_'))
            conn.close()
        finally:
            if listener is not None: flow.close_listener(listener)

    def test_loopback_extension_callback_remains_strictly_authorized(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        flow, listener = PkceFlow(binding(), port), None
        try:
            listener = flow.bind_listener()
            path = '/callback?' + urlencode({'state': flow.state, 'scope': SCOPE,
                'picked_file_ids': flow.binding.file_id, 'code': 'SYNTHETIC_ONLY_AUTHORIZATION_CODE',
                'extension_not_forwarded': 'synthetic-private-value'})
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            conn.request('GET', path); self.assertEqual(conn.getresponse().status, 204)
            self.assertTrue(flow.await_callback(listener, 3).startswith('SYNTHETIC_ONLY_'))
            self.assertEqual(flow.callback_diagnostic['unknown_field_count'], 1)
            self.assertNotIn('extension_not_forwarded', json.dumps(flow.callback_diagnostic))
            conn.close()
        finally:
            if listener is not None: flow.close_listener(listener)

if __name__ == '__main__': unittest.main()
