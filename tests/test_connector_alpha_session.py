"""Adversarial synthetic tests by A1 acceptance owner. Never calls real Google."""
import copy
from contextlib import nullcontext
import unittest
from urllib.parse import urlencode, urlparse, parse_qs
from test_connector_alpha_transport import binding
from companion_mind.connector_alpha.contract import Denied, MIME, SCOPE, require
from companion_mind.connector_alpha.oauth import PkceFlow
from companion_mind.connector_alpha.session import ConnectorSession


class Broker:
    def __init__(self):
        self.closed = False
        self.deleted = False
        self.available = True
        self.started = False
    def synchronized(self):
        return nullcontext()
    def begin_read(self):
        require(not self.started and not self.closed, 'READ_ALREADY_CONSUMED')
        self.started = True
    def assert_active(self):
        require(not self.closed and self.available, 'LOCAL_AUTHORIZATION_CLOSED')
    def close_local(self):
        self.closed = True
    def token_for_cleanup(self):
        require(self.closed and self.available, 'LOCAL_AUTHORIZATION_CLOSED')
        return 'SYNTHETIC_ONLY_REVIEW_TOKEN'
    def acquire(self):
        require(not self.closed and self.available, 'LOCAL_AUTHORIZATION_CLOSED')
        return 'SYNTHETIC_ONLY_REVIEW_TOKEN'
    def close_and_delete(self):
        self.closed = True
        self.deleted = True


class Provider:
    def __init__(self, b):
        self.b, self.calls = b, []
        self.body = {'documentId': b.file_id, 'revisionId': 'rev1', 'tabs': [
            {'tabProperties': {'tabId': 't1'}, 'documentTab': {'body': {'content': [
                {'paragraph': {'elements': [{'textRun': {'content': 'Synthetic safe text\n'}}]}}
            ]}}}]}
        self.version = '7'
        self.on_body = None
    def read(self, operation, token):
        self.calls.append(operation)
        if operation == 'identity':
            return {'user': {'emailAddress': self.b.subject_email,
                             'permissionId': self.b.subject_permission_id}}
        if operation in {'drive_before', 'drive_after'}:
            return {'id': self.b.file_id, 'mimeType': MIME, 'version': self.version,
                    'modifiedTime': '2026-09-21T10:00:00Z', 'trashed': False, 'isAppAuthorized': True}
        if operation in {'docs_before', 'docs_after'}:
            return {'documentId': self.b.file_id, 'revisionId': 'rev1'}
        if operation == 'body':
            if self.on_body:
                self.on_body()
            return copy.deepcopy(self.body)
        raise AssertionError('unexpected operation')
    def revoke(self, token):
        self.calls.append('revoke')


class SessionSafety(unittest.TestCase):
    def setUp(self):
        self.b, self.broker = binding(), Broker()
        self.provider = Provider(self.b)
        self.session = ConnectorSession(self.b, self.broker, self.provider)

    def assert_no_success(self, call):
        try:
            result = call()
        except Denied:
            return
        self.assertNotEqual('SUCCESS', result.get('receipt', result).get('status'))
        self.assertIsNone(result.get('content'))

    def test_revision_of_body_must_match(self):
        self.provider.body['revisionId'] = 'other-revision'
        self.assert_no_success(self.session.read_once)

    def test_missing_drive_version_cannot_succeed(self):
        self.provider.version = None
        self.assert_no_success(self.session.read_once)

    def test_invalid_child_tab_cannot_be_full(self):
        self.provider.body['tabs'][0]['childTabs'] = [{'tabProperties': {'tabId': 't2'}}]
        self.assert_no_success(self.session.read_once)

    def test_revoke_during_read_prevents_delivery(self):
        self.provider.on_body = self.broker.close_and_delete
        self.assert_no_success(self.session.read_once)

    def test_revoke_closes_even_if_acquisition_unavailable(self):
        self.broker.available = False
        try:
            self.session.revoke()
        except Denied:
            pass
        self.assertTrue(self.session.closed or self.broker.closed)

    def test_provider_token_echo_must_not_be_delivered(self):
        self.provider.body['tabs'][0]['documentTab']['body']['content'][0]['paragraph']['elements'][0]['textRun']['content'] = 'SYNTHETIC_ONLY_REVIEW_TOKEN'
        self.assert_no_success(self.session.read_once)

    def test_simple_document_with_standard_style_metadata(self):
        doc = self.provider.body['tabs'][0]['documentTab']
        doc.update(documentStyle={'background': {'color': {}}}, namedStyles={'styles': []},
                   lists={}, namedRanges={})
        result = self.session.read_once()
        self.assertEqual('SUCCESS', result['receipt']['status'])
        self.assertIsNotNone(result['content'])


class OAuthSafety(unittest.TestCase):
    def callback(self, flow, **changes):
        values = {'state': flow.state, 'scope': SCOPE,
                  'picked_file_ids': flow.binding.file_id, 'code': 'SYNTHETIC_ONLY_AUTHORIZATION_CODE'}
        values.update(changes)
        return flow.redirect_uri + '?' + urlencode(values)

    def test_duplicate_authorization_code_rejected(self):
        flow = PkceFlow(binding(), 18497)
        with self.assertRaises(Denied):
            flow.accept_callback(self.callback(flow) + '&code=SYNTHETIC_ONLY_SECOND_CODE')

    def test_callback_replay_rejected(self):
        flow = PkceFlow(binding(), 18497)
        callback = self.callback(flow)
        flow.accept_callback(callback)
        with self.assertRaises(Denied):
            flow.accept_callback(callback)

    def test_authorization_boundary_parameters(self):
        flow = PkceFlow(binding(), 18497)
        parsed = urlparse(flow.authorization_url)
        self.assertEqual(('https', 'accounts.google.com', '/o/oauth2/v2/auth'),
                         (parsed.scheme, parsed.hostname, parsed.path))
        q = parse_qs(parsed.query)
        for key, expected in {'scope': SCOPE, 'file_ids': flow.binding.file_id,
                              'mimetypes': MIME, 'code_challenge_method': 'S256',
                              'access_type': 'online', 'trigger_onepick': 'true'}.items():
            self.assertEqual([expected], q[key])
        self.assertNotIn('client_secret', q)
        self.assertNotIn('code_verifier', q)
        self.assertNotEqual(['true'], q.get('include_granted_scopes'))
        self.assertNotEqual(['true'], q.get('allow_multiple'))
        self.assertNotEqual(['true'], q.get('allow_folder_selection'))

    def test_wrong_scope_file_state_rejected(self):
        for changes in [{'scope': SCOPE + ' https://www.googleapis.com/auth/drive'},
                        {'picked_file_ids': 'synthetic-neighbor'}, {'state': 'wrong-state'}]:
            flow = PkceFlow(binding(), 18497)
            with self.subTest(fields=list(changes)), self.assertRaises(Denied):
                flow.accept_callback(self.callback(flow, **changes))


if __name__ == '__main__':
    unittest.main(verbosity=2)
