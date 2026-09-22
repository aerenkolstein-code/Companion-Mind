import unittest

from companion_mind.connector_s3.broker import FixedSyntheticDispatcher, PurposeBroker, RequestIntent
from companion_mind.connector_s3.policy import Denied, MemoryState
from companion_mind.connector_s3.stage import GenerationBinding, StageLifecycle


BODY = 'a' * 64


def binding():
    return GenerationBinding(1, 'account', 'subject', 'googleapis.com', 'client', 'project',
                             'drive.file', 'synthetic-sid', 0, 500, 'folder', 'file-a', 'file-b')


class Secrets:
    def __init__(self): self.calls = []
    def business_token(self, ref): self.calls.append(('business', ref)); return 'business-token-synthetic-12345'
    def refresh_token(self, ref): self.calls.append(('refresh', ref)); return 'refresh-token-synthetic-12345'
    def cleanup_token(self, ref): self.calls.append(('cleanup', ref)); return 'cleanup-token-synthetic-12345'


class PurposeBrokerTests(unittest.TestCase):
    def setUp(self):
        self.binding = binding()
        self.lifecycle = StageLifecycle(self.binding, MemoryState())
        self.lifecycle.initialize()
        self.secrets, self.dispatcher = Secrets(), FixedSyntheticDispatcher()
        self.broker = PurposeBroker(self.lifecycle, self.binding, self.secrets, 'synthetic-token-ref', self.dispatcher)

    def test_business_is_fixed_and_returns_no_token(self):
        intent = RequestIntent('POST', 'docs.batchUpdate', 'file-a', BODY, 'revision-1')
        pin = self.broker.pin_write('pin-a', intent, 'b' * 64, now=1)
        receipt = self.broker.business('write-a', intent, now=1, lane='normal', bucket='A_B_flow', file='A', intent_ref=pin)
        self.assertEqual((receipt.purpose, receipt.resource_id), ('business', 'file-a'))
        self.assertNotIn('business-token', repr(receipt))
        self.assertEqual(self.secrets.calls, [('business', 'synthetic-token-ref')])
        with self.assertRaisesRegex(Denied, 'RESOURCE_DENIED'):
            self.broker.business('wrong-resource', RequestIntent('POST', 'docs.batchUpdate', 'file-b', BODY, 'r'),
                                 now=1, lane='normal', bucket='A_B_flow', file='A', intent_ref='pin-a')

    def test_refresh_and_post_revoke_business_are_zero_dispatch(self):
        refresh = RequestIntent('POST', 'oauth.token.refresh', 'synthetic-token-ref', BODY, None)
        self.broker.refresh('refresh', refresh, now=1)
        self.broker.revoke_local()
        restarted = PurposeBroker(StageLifecycle(self.binding, self.lifecycle.store), self.binding,
                                  self.secrets, 'synthetic-token-ref', self.dispatcher)
        before = len(self.dispatcher.records)
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'):
            restarted.business('after', RequestIntent('POST', 'docs.batchUpdate', 'file-a', BODY, 'r'),
                               now=1, lane='normal', bucket='A_B_flow', file='A', intent_ref='pin')
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'):
            restarted.refresh('after-refresh', refresh, now=1)
        self.assertEqual(len(self.dispatcher.records), before)

    def test_only_fixed_cleanup_dispatches_after_revoke(self):
        self.broker.revoke_local()
        cleanup = RequestIntent('POST', 'oauth.token.revoke', 'synthetic-token-ref', BODY, None)
        receipt = self.broker.cleanup('cleanup', cleanup, now=1)
        self.assertEqual(receipt.purpose, 'cleanup')
        self.assertEqual(self.secrets.calls, [('cleanup', 'synthetic-token-ref')])
        with self.assertRaisesRegex(Denied, 'ENDPOINT_DENIED'):
            self.broker.cleanup('bad-cleanup', RequestIntent('POST', 'oauth.token.refresh', 'synthetic-token-ref', BODY, None), now=1)

    def test_intent_has_no_url_or_callback_entrypoint(self):
        with self.assertRaisesRegex(Denied, 'ENDPOINT_DENIED'):
            RequestIntent('POST', 'https://example.invalid', 'file-a', BODY, None)
        with self.assertRaisesRegex(Denied, 'INTENT_INVALID'):
            RequestIntent('GET', 'docs.batchUpdate', 'file-a', BODY, 'revision')


if __name__ == '__main__':
    unittest.main()
