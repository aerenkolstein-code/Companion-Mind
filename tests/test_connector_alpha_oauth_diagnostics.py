import json
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
        self.assertEqual((diagnostic['stage'], diagnostic['unknown_field_count']), ('QUERY_SHAPE', 1))
        self.assertNotIn('VERY_SECRET_CODE_VALUE', rendered)
        self.assertNotIn('wrong-secret-value', rendered)
        self.assertNotIn('unrecognized_secret_parameter', rendered)

    def test_duplicate_and_illegal_callbacks_have_bounded_stages(self):
        flow = self.flow()
        duplicate = flow.redirect_uri + '?state=' + flow.state + '&state=other&code=' + 'x' * 24
        result = flow.diagnose_callback(duplicate)
        self.assertEqual(result['stage'], 'QUERY_SHAPE')
        self.assertTrue(result['known_duplicate']['state'])
        self.assertEqual(flow.diagnose_callback('https://evil.invalid/callback')['stage'], 'HTTP_ORIGIN')
        self.assertEqual(flow.diagnose_callback('http://127.0.0.1:18765/not-callback')['stage'], 'HTTP_PATH')

if __name__ == '__main__': unittest.main()
