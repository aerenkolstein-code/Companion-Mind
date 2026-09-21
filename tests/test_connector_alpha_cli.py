"""No-network CLI flow tests with real binding and lifecycle state."""
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timezone
import io, json, shutil, unittest
from pathlib import Path
from unittest.mock import patch

from companion_mind.connector_alpha import cli
from companion_mind.connector_alpha.contract import Binding, SCOPE
from companion_mind.connector_alpha.secret_store import CredentialBroker
from tests.test_connector_alpha_lifecycle import MemoryStore, Gate

TOKEN = 'SYNTHETIC_ONLY_CLI_TOKEN_VALUE_0123456789'

class Flow:
    closed = 0
    def __init__(self, binding, port): self.binding, self.port, self.verifier, self.redirect_uri = binding, port, 'v', 'http://127.0.0.1:8765/callback'
    @property
    def authorization_url(self): return 'https://example.invalid/authorize'
    def bind_listener(self): return object()
    def await_callback(self, _): return 'SYNTHETIC_ONLY_AUTHORIZATION_CODE'
    @staticmethod
    def close_listener(_): Flow.closed += 1

class Transport:
    def __init__(self, binding): self.binding, self.counts = binding, {'oauth_exchanges': 0, 'google_reads': 0, 'remote_revocations': 0}
    def exchange_code(self, *_):
        self.counts['oauth_exchanges'] += 1
        return {'access_token': TOKEN, 'scope': SCOPE, 'token_type': 'Bearer', 'expires_in': 60}
    def read(self, op, token):
        self.counts['google_reads'] += 1
        b = self.binding
        if op == 'identity': return {'user': {'emailAddress': b.subject_email, 'permissionId': 'observed-id'}}
        if op.startswith('drive_'): return {'id': b.file_id, 'mimeType': 'application/vnd.google-apps.document', 'version': '7', 'modifiedTime': '2026-09-21T00:00:00Z', 'trashed': False, 'isAppAuthorized': True}
        if op.startswith('docs_'): return {'documentId': b.file_id, 'revisionId': 'r7'}
        return {'documentId': b.file_id, 'revisionId': 'r7', 'tabs': [
            {'tabProperties': {'tabId': 't'}, 'documentTab': {'body': {'content': [
                {'paragraph': {'elements': [{'textRun': {'content': 'safe text'}}]}}
            ]}}}
        ]}
    def revoke(self, _): self.counts['remote_revocations'] += 1

class CliFlow(unittest.TestCase):
    def setUp(self):
        self.store, self.gate = MemoryStore(), Gate()
        root = Path.cwd() / 'tests' / '.ca_cli_scratch'
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = root / self.id().replace('.', '-')
        self.tmp.mkdir(exist_ok=True)
        def cleanup():
            if self.tmp.resolve().parent == root.resolve(): shutil.rmtree(self.tmp, ignore_errors=True)
        self.addCleanup(cleanup)
        self.config, self.output = self.tmp / 'binding.json', self.tmp / 'content.json'
        b = Binding('cli-test', '123-test.apps.googleusercontent.com', 'project', 'CLI test', 'user@example.invalid', None, 'owner', 'universe', 'purpose', 'doc-1', 'S-1-5-21-1', '2099-01-01T00:00:00+00:00', True, True, True)
        self.config.write_text(json.dumps(asdict(b)))
        self.patches = [patch.object(cli, 'PkceFlow', Flow), patch.object(cli.webbrowser, 'open', return_value=True),
                        patch.object(cli, 'GoogleTransport', Transport),
                        patch.object(cli, 'CredentialBroker', lambda b: CredentialBroker(b, self.store, self.gate))]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    def run_cli(self, *args):
        sink = io.StringIO()
        with redirect_stdout(sink): code = cli.main(['--config', str(self.config), *args])
        return code, json.loads(sink.getvalue())

    def test_authorize_read_cleanup_and_replay_denied(self):
        code, auth = self.run_cli('authorize')
        self.assertEqual((code, auth['status'], auth['google_reads']), (0, 'AUTHORIZED', 1))
        self.assertNotIn(TOKEN, json.dumps(auth))
        self.assertEqual(json.loads(self.config.read_text())['subject_permission_id'], 'observed-id')
        code, read = self.run_cli('--content-out', str(self.output), 'read')
        self.assertEqual((code, read['content_delivered'], read['cleanup_credential_deleted']), (0, True, True))
        self.assertEqual(json.loads(self.output.read_text())['tabs'][0]['text'], 'safe text')
        self.assertEqual(self.run_cli('--content-out', str(self.output), 'read')[0], 2)

    def test_browser_failure_closes_listener(self):
        with patch.object(cli.webbrowser, 'open', return_value=False):
            code, receipt = self.run_cli('authorize')
        self.assertEqual((code, receipt['status']), (2, 'BLOCKED'))
        self.assertGreaterEqual(Flow.closed, 1)
        self.assertEqual(self.store.data, {})

if __name__ == '__main__': unittest.main()
