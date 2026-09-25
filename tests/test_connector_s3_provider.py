from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from companion_mind.connector_s3.docs_plan import canonical, replace_unique, parse_document
from companion_mind.connector_s3.oauth import SCOPE
from companion_mind.connector_s3.policy import Denied
from companion_mind.connector_s3.provider import GoogleProvider, ProviderFailure, DOC, FOLDER
from companion_mind.connector_s3.stage import GenerationBinding
from tests.test_connector_s3_docs_plan import document

TOKEN = 'synthetic-access-token-12345'
REFRESH = 'synthetic-refresh-token-12345'
SECRET = 'synthetic-client-secret-12345'


def binding():
    return GenerationBinding(1, 'synthetic@example.invalid', 'synthetic-subject', 'googleapis.com',
                             '123-synthetic.apps.googleusercontent.com', 'project', 'drive.file',
                             'synthetic-sid', 0, 100, 'folder', 'synthetic-doc', 'other-doc')


def metadata(resource, folder=False):
    return {'id': resource, 'mimeType': FOLDER if folder else DOC, 'parents': ['parent' if folder else 'folder'],
            'trashed': False, 'isAppAuthorized': True, 'capabilities': {'canEdit': True, 'canAddChildren': folder}}


class Response:
    def __init__(self, data=None, status=200, raw=None):
        self.status = status
        self.raw = raw if raw is not None else json.dumps(data if data is not None else {}).encode()
    def getheader(self, name): return 'application/json' if name == 'Content-Type' else None
    def read1(self, size):
        result, self.raw = self.raw[:size], self.raw[size:]
        return result


class Socket:
    def settimeout(self, value): assert 0 < value <= 30


class FakeHTTPS:
    instances, responses, failure, closes_after_headers = [], [], False, False
    def __init__(self, host, **kwargs):
        self.host, self.options, self.calls, self.closed = host, kwargs, [], False
        self.sock = Socket()
        self.instances.append(self)
    def connect(self): pass
    def request(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body, headers))
        if self.failure:
            raise OSError('synthetic-secret-in-error')
    def getresponse(self):
        if self.closes_after_headers:
            self.sock = None
        return self.responses.pop(0)
    def close(self): self.closed = True


class ProviderGrammar(unittest.TestCase):
    def setUp(self):
        FakeHTTPS.instances, FakeHTTPS.responses, FakeHTTPS.failure = [], [], False
        FakeHTTPS.closes_after_headers = False
        self.mock = patch('companion_mind.connector_s3.provider.http.client.HTTPSConnection', FakeHTTPS)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.provider = GoogleProvider(binding())

    def test_exact_identity_and_resource_metadata(self):
        FakeHTTPS.responses = [Response({'user': {'emailAddress': binding().account, 'permissionId': binding().subject}}), Response(metadata('synthetic-doc'))]
        self.assertEqual(self.provider.identity(TOKEN), {'verified': True})
        self.provider.metadata(TOKEN, 'synthetic-doc')
        self.assertEqual(FakeHTTPS.instances[1].host, 'www.googleapis.com')
        self.assertTrue(FakeHTTPS.instances[1].calls[0][1].startswith('/drive/v3/files/synthetic-doc?'))
        with self.assertRaisesRegex(Denied, 'RESOURCE_DENIED'):
            self.provider.metadata(TOKEN, 'unselected-neighbor')
        self.assertEqual(len(FakeHTTPS.instances), 2)

    def test_internal_dispatch_has_closed_action_grammar(self):
        for action in ('DELETE', 'drive.files.list', 'https://www.googleapis.com/drive/v3/files'):
            with self.assertRaisesRegex(Denied, 'ENDPOINT_DENIED'):
                self.provider._send(action, token=TOKEN, resource_id='synthetic-doc')
        with self.assertRaisesRegex(Denied, 'TOKEN_FORM_DENIED'):
            self.provider._send('refresh', form_data={'client_id': 'wrong'})
        with self.assertRaisesRegex(Denied, 'WRITE_PLAN_DENIED'):
            self.provider._send('write', token=TOKEN)
        self.assertEqual(FakeHTTPS.instances, [])

    def test_connection_close_response_keeps_body_timeout_socket(self):
        FakeHTTPS.closes_after_headers = True
        FakeHTTPS.responses = [Response({'user': {'emailAddress': binding().account, 'permissionId': binding().subject}})]
        self.assertEqual(self.provider.identity(TOKEN), {'verified': True})

    def test_parent_and_edit_capability_changes_fail(self):
        bad = metadata('synthetic-doc'); bad['parents'] = ['other-folder']
        FakeHTTPS.responses = [Response(bad)]
        with self.assertRaisesRegex(Denied, 'RESOURCE_PARENT_DENIED'):
            self.provider.metadata(TOKEN, 'synthetic-doc')
        bad = metadata('synthetic-doc'); bad['capabilities']['canEdit'] = False
        FakeHTTPS.responses = [Response(bad)]
        with self.assertRaisesRegex(Denied, 'RESOURCE_CAPABILITY_DENIED'):
            self.provider.metadata(TOKEN, 'synthetic-doc')

    def test_full_document_get_and_conditional_write(self):
        FakeHTTPS.responses = [Response(document('hello 😀\n')), Response({'writeControl': {'requiredRevisionId': 'next'}})]
        before = self.provider.document(TOKEN, 'synthetic-doc')
        self.assertIn('includeTabsContent=true', FakeHTTPS.instances[0].calls[0][1])
        self.assertIn('suggestionsViewMode=SUGGESTIONS_INLINE', FakeHTTPS.instances[0].calls[0][1])
        plan = replace_unique(before, 'hello', 'changed')
        self.provider.write_document(TOKEN, plan)
        sent = FakeHTTPS.instances[1].calls[0]
        self.assertEqual((sent[0], sent[1]), ('POST', '/v1/documents/synthetic-doc:batchUpdate'))
        self.assertEqual(sent[2], plan.request_bytes)
        self.assertEqual(json.loads(sent[2])['writeControl']['requiredRevisionId'], before.revision)

    def test_arbitrary_batch_and_mismatched_revision_are_rejected_before_connection(self):
        before = parse_document(document('hello\n'), 'synthetic-doc')
        plan = replace_unique(before, 'hello', 'changed')
        bad = {'requests': [{'deleteTab': {'tabId': 't.0'}}], 'writeControl': {'requiredRevisionId': before.revision}}
        with self.assertRaisesRegex(Denied, 'WRITE_PLAN_DENIED'):
            self.provider.write_document(TOKEN, replace(plan, request_bytes=canonical(bad)))
        bad = json.loads(plan.request_bytes); bad['writeControl']['requiredRevisionId'] = 'other'
        with self.assertRaisesRegex(Denied, 'WRITE_PLAN_DENIED'):
            self.provider.write_document(TOKEN, replace(plan, request_bytes=canonical(bad)))
        self.assertEqual(FakeHTTPS.instances, [])

    def test_creation_is_one_attempt_and_requires_metadata_readback_for_admission(self):
        FakeHTTPS.responses = [Response(metadata('created-c')), Response(metadata('created-c'))]
        created = self.provider.create_c(TOKEN)
        with self.assertRaisesRegex(Denied, 'RESOURCE_DENIED'):
            self.provider.document(TOKEN, 'created-c')
        readback = self.provider.metadata(TOKEN, created['id'])
        self.assertEqual(self.provider.admit_created_c(readback), 'created-c')
        with self.assertRaisesRegex(Denied, 'CREATE_ALREADY_ATTEMPTED'):
            self.provider.create_c(TOKEN)
        self.assertEqual(len(FakeHTTPS.instances), 2)
        self.assertEqual(json.loads(FakeHTTPS.instances[0].calls[0][2])['parents'], ['folder'])

    def test_fixed_oauth_forms_exact_scope_and_no_bearer_header(self):
        response = {'token_type': 'Bearer', 'scope': SCOPE, 'expires_in': 3600,
                    'access_token': TOKEN, 'refresh_token': REFRESH}
        FakeHTTPS.responses = [Response(response), Response(response), Response()]
        self.provider.exchange('synthetic-code-1234567890', 'a'*64, 'http://127.0.0.1:54321/callback', SECRET)
        self.provider.refresh(REFRESH, SECRET)
        self.provider.revoke(REFRESH)
        self.assertEqual([c.host for c in FakeHTTPS.instances], ['oauth2.googleapis.com']*3)
        self.assertEqual([c.calls[0][1] for c in FakeHTTPS.instances], ['/token', '/token', '/revoke'])
        self.assertTrue(all('Authorization' not in c.calls[0][3] for c in FakeHTTPS.instances))
        self.assertEqual(parse_qs(FakeHTTPS.instances[1].calls[0][2].decode())['grant_type'], ['refresh_token'])

    def test_redirect_and_transport_failure_never_retry_or_echo_error_text(self):
        FakeHTTPS.responses = [Response(status=302)]
        with self.assertRaisesRegex(ProviderFailure, '^PROVIDER_ERROR$'):
            self.provider.identity(TOKEN)
        self.assertEqual(len(FakeHTTPS.instances), 1)
        FakeHTTPS.failure = True
        with self.assertRaisesRegex(ProviderFailure, '^TRANSPORT_UNKNOWN$'):
            self.provider.identity(TOKEN)
        self.assertEqual(len(FakeHTTPS.instances), 2)
        self.assertTrue(all(c.closed for c in FakeHTTPS.instances))

    def test_response_limit_is_enforced(self):
        FakeHTTPS.responses = [Response(raw=b'x'*1048577)]
        with self.assertRaisesRegex(Denied, 'RESPONSE_LIMIT'):
            self.provider.identity(TOKEN)

    def test_provider_rejection_has_only_fixed_status(self):
        FakeHTTPS.responses = [Response(raw=b'synthetic-secret-error-body', status=403)]
        with self.assertRaises(ProviderFailure) as captured:
            self.provider.identity(TOKEN)
        self.assertEqual(str(captured.exception), 'ACCESS_DENIED')
        self.assertEqual(captured.exception.http_status, 403)


if __name__ == '__main__':
    unittest.main()
