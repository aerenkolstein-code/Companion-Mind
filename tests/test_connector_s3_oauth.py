from urllib.parse import urlencode, parse_qs, urlparse
import unittest
from companion_mind.connector_s3.oauth import PkceFlow, SCOPE, ISSUER
from companion_mind.connector_s3.stage import GenerationBinding
from companion_mind.connector_s3.policy import Denied


def flow():
    return PkceFlow(GenerationBinding(1, 'synthetic-account', 'synthetic-subject', 'googleapis.com',
                    'client', 'project', 'drive.file', 'synthetic-sid', 0, 100,
                    'folder', 'file-a', 'file-b'), 54321)


def callback(f, **overrides):
    fields = {'state': f.state, 'code': 'synthetic-code-0123456789', 'scope': SCOPE,
              'picked_file_ids': 'folder,file-a,file-b', 'iss': ISSUER}
    fields.update(overrides)
    return f.redirect_uri + '?' + urlencode(fields)


class S3OAuth(unittest.TestCase):
    def test_offline_exact_selection_parameters_no_listener_or_network(self):
        f = flow()
        params = parse_qs(urlparse(f.authorization_url).query)
        self.assertEqual(params['scope'], [SCOPE])
        self.assertEqual(params['access_type'], ['offline'])
        self.assertEqual(params['allow_multiple'], ['true'])
        self.assertEqual(params['allow_folder_selection'], ['true'])
        self.assertEqual(params['file_ids'], ['folder,file-a,file-b'])
        self.assertEqual(params['code_challenge_method'], ['S256'])

    def test_exact_set_accepts_any_order_once(self):
        f = flow()
        self.assertEqual(f.accept_callback(callback(f, picked_file_ids='file-b,folder,file-a')), 'synthetic-code-0123456789')
        with self.assertRaisesRegex(Denied, 'CALLBACK_REPLAYED'):
            f.accept_callback(callback(f))

    def test_extra_missing_or_duplicate_selection_rejected(self):
        for value in ('folder,file-a', 'folder,file-a,file-b,neighbor', 'folder,file-a,file-a'):
            f = flow()
            with self.assertRaisesRegex(Denied, 'PICKER_FILE_MISMATCH'):
                f.accept_callback(callback(f, picked_file_ids=value))

    def test_scope_issuer_state_origin_and_duplicate_parameters_rejected(self):
        for override in ({'scope': SCOPE + ' other'}, {'iss': 'https://example.invalid'}, {'state': 'wrong'}):
            f = flow()
            with self.assertRaises(Denied):
                f.accept_callback(callback(f, **override))
        f = flow()
        with self.assertRaisesRegex(Denied, 'CALLBACK_DUPLICATE_PARAMETER'):
            f.accept_callback(callback(f) + '&state=duplicate')
        f = flow()
        with self.assertRaisesRegex(Denied, 'CALLBACK_INVALID'):
            f.accept_callback(callback(f).replace('127.0.0.1:', 'attacker@127.0.0.1:'))

    def test_diagnostics_never_contain_callback_secrets(self):
        f = flow()
        f.accept_callback(callback(f))
        diagnostic = repr(f.diagnose_callback(callback(f)))
        self.assertNotIn('synthetic-code', diagnostic)
        self.assertNotIn(f.state, diagnostic)
        self.assertNotIn(f.verifier, diagnostic)


if __name__ == '__main__':
    unittest.main()
