"""Synthetic S3 read-boundary tests; HTTPS is replaced at the provider edge."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from companion_mind.connector_alpha.secret_store import SessionGate, current_sid
from companion_mind.connector_s3.credentials import NativeCredentials
from companion_mind.connector_s3.native import NativeLedger, PREFIX, S3CredentialStore
from companion_mind.connector_s3.policy import Denied, MemoryState
from companion_mind.connector_s3.provider import GoogleProvider, ProviderFailure
from companion_mind.connector_s3.runtime import S3Runtime
from companion_mind.connector_s3.stage import StageLifecycle
from tests.test_connector_s3_credentials import Store
from tests.test_connector_s3_docs_plan import document
from tests.test_connector_s3_provider import FakeHTTPS, Response, TOKEN, REFRESH, binding, metadata


class RuntimeReads(unittest.TestCase):
    target = PREFIX + 'Synthetic.Runtime.Tokens'

    def setUp(self):
        self.bound = binding()
        self.stage = StageLifecycle(self.bound, MemoryState()); self.stage.initialize()
        self.native = Store()
        self.credentials = NativeCredentials(self.native, self.stage, self.bound, self.target, lambda: True)
        self.credentials.rotate(TOKEN, REFRESH, 50, 1, now=1)
        self.provider = GoogleProvider(self.bound)
        self.runtime = S3Runtime(self.stage, self.bound, self.credentials, self.provider, clock=lambda: 1)
        FakeHTTPS.instances, FakeHTTPS.responses, FakeHTTPS.failure = [], [], False
        FakeHTTPS.closes_after_headers = False
        mock = patch('companion_mind.connector_s3.provider.http.client.HTTPSConnection', FakeHTTPS)
        mock.start(); self.addCleanup(mock.stop)

    def test_fixed_identity_metadata_document_charge_before_dispatch(self):
        FakeHTTPS.responses = [
            Response({'user': {'emailAddress': self.bound.account, 'permissionId': self.bound.subject}}),
            Response(metadata(self.bound.folder_id, folder=True)),
            Response(metadata(self.bound.a_id)),
            Response(metadata(self.bound.folder_id, folder=True)),
            Response(metadata(self.bound.a_id)),
            Response(document('synthetic body\n')),
        ]
        self.assertEqual(self.runtime.identity('identity')['status'], 'VERIFIED')
        self.assertEqual(self.runtime.metadata('metadata', self.bound.a_id)['status'], 'VERIFIED')
        result = self.runtime.document('read', self.bound.a_id)
        self.assertEqual(result['status'], 'READ')
        self.assertEqual(len(result['text_sha256']), 64)
        self.assertNotIn('synthetic body', str(result))
        totals = self.stage.snapshot()['totals']
        self.assertEqual((totals['api_total'], totals['buckets']['initialization'],
                          totals['buckets']['A_B_flow']), (6, 1, 5))
        self.assertEqual(len(FakeHTTPS.instances), 6)

    def test_invalid_resource_revoke_session_and_expiry_zero_http(self):
        with self.assertRaisesRegex(Denied, 'RUNTIME_RESOURCE_DENIED'):
            self.runtime.document('outside', 'unselected-neighbor')
        locked = NativeCredentials(self.native, self.stage, self.bound, self.target, lambda: False)
        with self.assertRaisesRegex(Denied, 'SESSION_GATE_DENIED'):
            S3Runtime(self.stage, self.bound, locked, GoogleProvider(self.bound), clock=lambda: 1).identity('locked')
        expired = S3Runtime(self.stage, self.bound, self.credentials, GoogleProvider(self.bound), clock=lambda: 50)
        with self.assertRaisesRegex(Denied, 'CREDENTIAL_RECOVERY_REQUIRED'):
            expired.identity('expired')
        self.stage.revoke_local(self.bound)
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'):
            self.runtime.identity('revoked')
        self.assertEqual(len(FakeHTTPS.instances), 0)
        self.assertEqual(self.stage.snapshot()['totals']['api_total'], 2)

    def test_exhausted_batch_and_competing_instances(self):
        for number in range(48):
            self.stage.reserve_request(self.bound, 'fill-%d' % number, now=1,
                                       lane='normal', bucket='A_B_flow')
        with self.assertRaisesRegex(Denied, 'API_BUCKET_EXHAUSTED'):
            self.runtime.document('over', self.bound.a_id)
        self.assertEqual(len(FakeHTTPS.instances), 0)
        self.assertEqual(self.stage.snapshot()['totals']['buckets']['A_B_flow'], 50)

        # A fresh stage with one remaining slot proves shared lock/accounting.
        stage = StageLifecycle(self.bound, MemoryState()); stage.initialize()
        for number in range(49):
            stage.reserve_request(self.bound, 'compete-%d' % number, now=1,
                                  lane='normal', bucket='A_B_flow')
        native = Store()
        credential = NativeCredentials(native, stage, self.bound, self.target, lambda: True)
        credential.rotate(TOKEN, REFRESH, 50, 1, now=1)
        FakeHTTPS.responses = [Response(metadata(self.bound.folder_id, folder=True))]
        runtimes = [S3Runtime(stage, self.bound, credential, GoogleProvider(self.bound), clock=lambda: 1)
                    for _ in range(2)]
        def one(index):
            try:
                runtimes[index].metadata('race-%d' % index, self.bound.folder_id)
                return 'ALLOWED'
            except Denied:
                return 'DENIED'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(one, (0, 1)))
        self.assertEqual(sorted(outcomes), ['ALLOWED', 'DENIED'])
        self.assertEqual(len(FakeHTTPS.instances), 1)

    def test_provider_failure_charged_without_raw_error(self):
        FakeHTTPS.responses = [Response(status=503)]
        with self.assertRaisesRegex(ProviderFailure, '^PROVIDER_ERROR$'):
            self.runtime.identity('failed')
        self.assertEqual(self.stage.snapshot()['totals']['api_total'], 1)
        self.assertEqual(len(FakeHTTPS.instances), 1)

    def test_parent_and_folder_change_stop_before_document_http(self):
        bad_folder = metadata(self.bound.folder_id, folder=True)
        bad_folder['capabilities']['canAddChildren'] = False
        FakeHTTPS.responses = [Response(bad_folder)]
        with self.assertRaisesRegex(Denied, 'RESOURCE_CAPABILITY_DENIED'):
            self.runtime.document('folder-changed', self.bound.a_id)
        self.assertEqual(len(FakeHTTPS.instances), 1)
        self.assertEqual(self.stage.snapshot()['totals']['api_total'], 3)

        bad_file = metadata(self.bound.b_id)
        bad_file['parents'] = ['outside']
        FakeHTTPS.responses = [Response(metadata(self.bound.folder_id, folder=True)), Response(bad_file)]
        with self.assertRaisesRegex(Denied, 'RESOURCE_PARENT_DENIED'):
            self.runtime.document('parent-changed', self.bound.b_id)
        self.assertEqual(len(FakeHTTPS.instances), 3)
        self.assertEqual(self.stage.snapshot()['totals']['api_total'], 6)

    def test_exact_provider_and_credential_types_required(self):
        with self.assertRaisesRegex(Denied, 'RUNTIME_CONFIGURATION_DENIED'):
            S3Runtime(self.stage, self.bound, self.credentials, object(), clock=lambda: 1)
        with self.assertRaisesRegex(Denied, 'RUNTIME_CONFIGURATION_DENIED'):
            S3Runtime(self.stage, self.bound, object(), self.provider, clock=lambda: 1)
        self.assertEqual(len(FakeHTTPS.instances), 0)


@unittest.skipUnless(os.name == 'nt', 'Windows native canary')
class NativeRuntimeCanary(unittest.TestCase):
    def test_fresh_uuid_native_stage_token_and_session(self):
        sid = current_sid()
        bound = replace(binding(), sid=sid)
        native = S3CredentialStore(sid)
        stem = PREFIX + 'Synthetic.' + str(uuid4())
        stage_target, token_target = stem + '.Stage', stem + '.Tokens'
        self.assertIsNone(native.read_record(stage_target, persist=2))
        self.assertIsNone(native.read_record(token_target, persist=2))
        try:
            with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                stage = StageLifecycle(bound, NativeLedger(Path(directory) / 'ledger', native, stage_target, stem))
                stage.initialize()
                credentials = NativeCredentials(native, stage, bound, token_target, SessionGate(sid))
                credentials.rotate(TOKEN, REFRESH, 50, 1, now=1)
                FakeHTTPS.instances, FakeHTTPS.responses, FakeHTTPS.failure = [], [
                    Response({'user': {'emailAddress': bound.account, 'permissionId': bound.subject}})], False
                FakeHTTPS.closes_after_headers = False
                with patch('companion_mind.connector_s3.provider.http.client.HTTPSConnection', FakeHTTPS):
                    runtime = S3Runtime(stage, bound, credentials, GoogleProvider(bound), clock=lambda: 1)
                    self.assertEqual(runtime.identity('native-identity')['status'], 'VERIFIED')
                    self.assertEqual(len(FakeHTTPS.instances), 1)
                    reopened = StageLifecycle(bound, NativeLedger(Path(directory) / 'ledger', native,
                                                                   stage_target, stem))
                    reopened.revoke_local(bound)
                    again = NativeCredentials(native, reopened, bound, token_target, SessionGate(sid))
                    with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'):
                        S3Runtime(reopened, bound, again, GoogleProvider(bound), clock=lambda: 1).identity('after-revoke')
                    self.assertEqual(len(FakeHTTPS.instances), 1)
        finally:
            native.delete(token_target)
            native.delete(token_target + '.Meta')
            native.delete(stage_target)
        for target in (token_target, token_target + '.Meta', stage_target):
            self.assertIsNone(native.read_record(target, persist=2))
