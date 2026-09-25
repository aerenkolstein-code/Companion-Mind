import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from companion_mind.connector_s3.policy import Denied
from companion_mind.connector_s3.runner import main
from companion_mind.connector_s3.transport import FixedRequest, MockFixedTransport


class FixedTransportTests(unittest.TestCase):
    def test_only_fixed_actions_and_no_retry_surface(self):
        request = FixedRequest('docs.batchUpdate', 'POST', 'synthetic-doc', 'a' * 64, 'r1')
        transport = MockFixedTransport()
        self.assertEqual(transport.dispatch(request), {'status': 'MOCK_OK'})
        self.assertEqual(set(transport.records[0]), {'action', 'intent_hash'})
        with self.assertRaisesRegex(Denied, 'FIXED_REQUEST_DENIED'):
            FixedRequest('https://example.invalid', 'POST', 'x', 'a' * 64)
        with self.assertRaisesRegex(Denied, 'FIXED_TRANSPORT_DENIED'):
            transport.dispatch({'url': 'https://example.invalid'})

    def test_cli_is_offline_and_secret_free(self):
        output = StringIO()
        with redirect_stdout(output): self.assertEqual(main(['mock-transaction']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual((result['mode'], result['real_oauth'], result['real_api']), ('LOCAL_ONLY', 0, 0))
        self.assertEqual(set(result), {'mode', 'real_oauth', 'real_api', 'intent_hash', 'transport_status'})


if __name__ == '__main__':
    unittest.main()
