"""Real loopback HTTP tests with synthetic authorization codes only."""
import http.client
import socket
import unittest
from urllib.parse import urlencode

from companion_mind.connector_alpha.contract import Denied, SCOPE
from companion_mind.connector_alpha.oauth import PkceFlow
from test_connector_alpha_transport import binding


class LoopbackTests(unittest.TestCase):
    def flow(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        flow = PkceFlow(binding(), port)
        listener = flow.bind_listener()
        self.addCleanup(flow.close_listener, listener)
        return flow, listener

    def path(self, flow):
        return '/callback?' + urlencode({'state': flow.state, 'scope': SCOPE,
            'picked_file_ids': flow.binding.file_id, 'code': 'SYNTHETIC_ONLY_LOOPBACK_AUTH_CODE'})

    def test_real_loopback_callback(self):
        flow, listener = self.flow()
        conn = http.client.HTTPConnection('127.0.0.1', flow.port, timeout=3)
        try:
            conn.request('GET', self.path(flow))
            self.assertEqual(204, conn.getresponse().status)
            self.assertTrue(flow.await_callback(listener, 3).startswith('SYNTHETIC_ONLY_'))
        finally:
            conn.close()

    def test_wrong_host_rejected(self):
        flow, listener = self.flow()
        conn = http.client.HTTPConnection('127.0.0.1', flow.port, timeout=3)
        try:
            conn.request('GET', self.path(flow), headers={'Host': 'example.invalid'})
            self.assertEqual(400, conn.getresponse().status)
            with self.assertRaises(Denied):
                flow.await_callback(listener, 3)
        finally:
            conn.close()

    def test_duplicate_host_rejected(self):
        flow, listener = self.flow()
        conn = http.client.HTTPConnection('127.0.0.1', flow.port, timeout=3)
        try:
            conn.putrequest('GET', self.path(flow))
            conn.putheader('Host', '127.0.0.1:' + str(flow.port))
            conn.endheaders()
            self.assertEqual(400, conn.getresponse().status)
            with self.assertRaises(Denied):
                flow.await_callback(listener, 3)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
