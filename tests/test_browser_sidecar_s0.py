"""WO-ENG-C1-A018-S0-01: C1 S0 offline checks, not independent acceptance.

Run directly with --receipt <temp-path> for a machine-readable return receipt.
Fixtures are authored expectations; no live browser or provider is used.
"""
import argparse
import ast
from copy import deepcopy
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from companion_mind.browser_sidecar import (CaptureError, load_profile, parse_dom,
                                            reconcile_conversation, replay_captures)
from companion_mind.browser_sidecar.normalize import calibrate_window, fingerprint
from companion_mind.browser_sidecar.pins import (ADAPTER, ARCHITECTURE_ID, BASE_SHA,
                                               CONTRACT_HASHES, PLAN_ID, PROVENANCE, verify_pins)
from companion_mind.browser_sidecar.vendor_profile import validate_profile
from companion_mind.journal import Journal
from companion_mind.journal.codec import JournalError, load_schema, prepare

FIXTURES = ROOT / 'tests/fixtures/c1_chatgpt_web_v0_1'
DOCS = ROOT / 'docs/browser_sidecar'
MANIFEST = json.loads((FIXTURES / 'manifest.json').read_text())

A029_P3S1_MUTABLE_OWNED_HOME = frozenset({
    'companion_mind/owned_home/source_pack.py',
    'companion_mind/owned_home/readonly_session.py',
    'companion_mind/owned_home/runtime.py',
    'companion_mind/owned_home/testport.py',
    'companion_mind/owned_home/shell.py',
    'companion_mind/owned_home/trace.py',
})


def c1_a029_frozen_predicate(path):
    """Preserve C1 S0 pins except the exact six later-authority A029 paths."""
    return ('chatgpt_recovery' not in path and
            path != 'docs/c2-recovery-prototype.md' and
            path not in A029_P3S1_MUTABLE_OWNED_HOME)



def fixture(n):
    return json.loads((FIXTURES / f'F{n:02}.json').read_text())


def parse_snapshot(s):
    return parse_dom(s['html'], contract_version=s.get('contract_version', 'canonical_event/v1'))


def scan_source():
    """Inspect executable import/call surfaces, not incidental docstring words."""
    denied_modules = {'socket', 'requests', 'http', 'urllib', 'subprocess', 'os', 'ctypes', 'importlib',
                      'selenium', 'playwright', 'webbrowser', 'google', 'sqlite3'}
    denied_calls = {'eval', 'exec', '__import__', 'fetch', 'XMLHttpRequest', 'webRequest', 'ingest',
                    'write_text', 'write_bytes', 'unlink', 'mkdir', 'connect', 'system'}
    hits = []
    for p in sorted((ROOT / 'companion_mind/browser_sidecar').glob('*.py')):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Import):
                if any(x.name.split('.')[0] in denied_modules for x in node.names):
                    hits.append([p.name, node.lineno, 'import'])
            if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] in denied_modules:
                hits.append([p.name, node.lineno, 'import'])
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
                if name in denied_calls:
                    hits.append([p.name, node.lineno, name])
                if name == 'open':
                    # Only the explicit read-only profile loader is allowed.
                    if p.name != 'vendor_profile.py' or any(k.arg == 'mode' and getattr(k.value, 'value', 'r') != 'r' for k in node.keywords):
                        hits.append([p.name, node.lineno, 'open'])
    return hits


class S0Tests(unittest.TestCase):
    def check_snapshot(self, s):
        if 'error' in s:
            with self.assertRaisesRegex(CaptureError, '^'+s['error']+'$'):
                parse_snapshot(s)
            return ()
        result = parse_snapshot(s)
        expected = s['expected']
        self.assertEqual(len(result), expected['count'])
        self.assertEqual([x.payload for x in result], expected['payloads'])
        self.assertEqual([x.role for x in result], expected['roles'])
        self.assertEqual([x.control_state for x in result], expected['control_states'])
        self.assertEqual([x.terminal_observation for x in result], expected['terminals'])
        if 'attachment_statuses' in expected:
            self.assertEqual([a.capture_status for x in result for a in x.attachments], expected['attachment_statuses'])
        if 'redaction_state' in expected:
            self.assertEqual(result[0].redaction_state, expected['redaction_state'])
        for x in result:
            self.assertFalse(x.to_dict()['canonical_commit'])
            self.assertFalse(x.to_dict()['authority'])
            self.assertEqual(x.to_dict()['ingest_state'], 'NOT_ATTEMPTED')
            self.assertNotIn('event_id', x.to_dict())
        return result

    def check_fixture(self, n):
        f = fixture(n)
        result = [self.check_snapshot(s) for s in f['snapshots']]
        encoded = json.dumps([[x.to_dict() for x in batch] for batch in result])
        for value in f.get('forbidden_output_strings', []):
            self.assertNotIn(value, encoded)
        return result

    def test_g0_01_execution_baseline_pin_guard(self):
        receipt = verify_pins(ROOT, BASE_SHA)
        self.assertEqual(receipt['architecture'], ARCHITECTURE_ID)
        self.assertEqual(receipt['plan'], PLAN_ID)
        with self.assertRaisesRegex(CaptureError, '^BASELINE_MOVED$'):
            verify_pins(ROOT, '0'*40)
        self.assertEqual(json.loads((DOCS/'s0-baseline.json').read_text())['base_sha'], BASE_SHA)

    def test_g0_02_contract_version_hashes_and_offline_seam(self):
        self.assertEqual(PROVENANCE, ('browser_sidecar', 'observed'))
        self.assertEqual(ADAPTER, 'A018')
        for path, digest in CONTRACT_HASHES.items():
            self.assertEqual(hashlib.sha256((ROOT/path).read_bytes()).hexdigest(), digest)
        schema = load_schema()
        event = json.loads((ROOT/'tests/fixtures/canonical_event_v1/03_browser_sidecar_multimodal.json').read_text())
        prepared = prepare(event, schema)
        self.assertEqual(prepared['source_ref']['source_kind'], PROVENANCE[0])
        self.assertEqual(list(inspect.signature(Journal.ingest).parameters), ['self', 'adapter', 'event'])
        # Existing published fixture through existing Journal, temporary offline store only.
        with tempfile.TemporaryDirectory() as d:
            with Journal(d) as journal:
                first = journal.ingest('A018', event)
                self.assertEqual(first['disposition'], 'COMMITTED')
                self.assertEqual(journal.ingest('A018', event)['disposition'], 'ALREADY_COMMITTED')
                with self.assertRaisesRegex(JournalError, 'ADAPTER_CONTRACT_MISMATCH'):
                    journal.ingest('A019', event)

    def test_g0_03_manifest_completeness_and_fingerprints(self):
        self.assertTrue(MANIFEST['synthetic'])
        self.assertFalse(MANIFEST['production_dom_verified'])
        self.assertEqual([e['fixture_id'] for e in MANIFEST['fixtures']], [f'F{i:02}' for i in range(1,21)])
        self.assertEqual({p.name for p in FIXTURES.glob('F*.json')}, {e['path'] for e in MANIFEST['fixtures']})
        self.assertEqual(fingerprint({k:v for k,v in MANIFEST.items() if k != 'manifest_fingerprint'}), MANIFEST['manifest_fingerprint'])
        for entry in MANIFEST['fixtures']:
            p = FIXTURES/entry['path']
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), entry['sha256'])
            f = json.loads(p.read_text())
            self.assertEqual(f['fixture_id'], entry['fixture_id'])
            self.assertTrue(f['snapshots'])
            self.assertTrue(all(('error' in s) != ('expected' in s) for s in f['snapshots']))

    def test_g0_04_deterministic_static_parse(self):
        for i in range(1,21):
            with self.subTest(fixture=i):
                a, b = self.check_fixture(i), self.check_fixture(i)
                self.assertEqual(a, b)
        # Reject malformed, nested, duplicated-attribute and multiple-root inputs atomically.
        valid = fixture(1)['snapshots'][0]['html']
        bad_inputs = [valid[:-7], valid+valid, valid+'\ud800', valid.replace('<article ', '<article hidden="false" hidden="true" '),
                      valid.replace('</article>', '<article data-c1-message="nested"></article></article>')]
        for bad in bad_inputs:
            with self.assertRaises(CaptureError):
                parse_dom(bad)

    def test_g0_05_role_extraction_and_no_prose_inference(self):
        self.check_fixture(1)
        self.check_fixture(2)
        html = fixture(1)['snapshots'][0]['html']
        self.assertEqual(parse_dom(html.replace('Hello world', 'I am an assistant persona'))[0].role, 'user')
        for bad in [html.replace('data-c1-role="user"', 'data-c1-role="system"'),
                    html.replace('data-c1-role-label="user"', 'data-c1-role-label="assistant"')]:
            with self.assertRaisesRegex(CaptureError, 'AMBIGUOUS_ROLE'):
                parse_dom(bad)

    def test_g0_06_normalized_visible_payload_and_terminal_evidence(self):
        self.check_fixture(3)
        self.check_fixture(4)
        self.check_fixture(5)
        self.check_fixture(6)
        plain = fixture(1)['snapshots'][0]['html']
        normalized = parse_dom(plain.replace('Hello world', 'Cafe\u0301\r\nsecond line'))[0]
        self.assertEqual(normalized.payload, 'Caf\u00e9\nsecond line')
        self.assertEqual(normalized.redaction_state, 'none')
        s = fixture(6)['snapshots'][0]['html']
        self.assertIn('    y = 2', parse_dom(s)[0].payload)
        s = fixture(2)['snapshots'][0]['html']
        with self.assertRaisesRegex(CaptureError, 'PROFILE_DRIFT'):
            parse_dom(s.replace('data-c1-terminal="complete"', ''))
        with self.assertRaisesRegex(CaptureError, 'AMBIGUOUS_TERMINAL'):
            parse_dom(s.replace('Synthetic response','').replace('data-c1-state="complete"','data-c1-state="complete"'))
        # Lifecycle UNKNOWN is never a terminal status.
        with self.assertRaisesRegex(CaptureError, 'PROFILE_DRIFT'):
            parse_dom(s.replace('data-c1-state="complete"','data-c1-state="UNKNOWN"'))

    def test_g0_07_attachment_reference(self):
        result = self.check_fixture(7)[0][0]
        a = result.attachments[0]
        self.assertEqual(a.filename, 'synthetic.csv')
        self.assertEqual(a.media_type, 'text/csv')
        self.assertEqual(a.source_ref, 'message:m-01:attachment:att-01')
        self.assertNotIn('https:', json.dumps(result.to_dict()))
        self.check_fixture(18)

    def test_g0_08_virtualization_dedupe(self):
        self.assertEqual(len(self.check_fixture(8)[0]), 1)
        html = fixture(8)['snapshots'][0]['html']
        # A conflicting rebuilt node must not be selected by recency/order.
        html = html.replace('Hello world','changed',1)
        with self.assertRaisesRegex(CaptureError, '^CONFLICT$'):
            parse_dom(html)

    def test_g0_09_reload_fixture_stability(self):
        a,b = self.check_fixture(9)
        self.assertEqual(a,b)
        self.assertEqual(len(replay_captures(a+b)),1)

    def test_g0_10_duplicate_tab_fixture_stability(self):
        a,b = self.check_fixture(10)
        self.assertEqual(a,b)
        self.assertEqual(len(replay_captures(a+b)),1)
        # A different scoped account must never silently collapse into this one.
        other = replace(a[0], scope_alias='another-local-alias')
        self.assertEqual(len(replay_captures(a+(other,))),2)

    def test_g0_11_same_id_same_fingerprint(self):
        a,b = self.check_fixture(11)
        self.assertEqual(a,b)
        self.assertEqual(replay_captures(a+b+a),a)

    def test_g0_12_same_id_different_fingerprint_conflict(self):
        a,b = self.check_fixture(12)
        self.assertEqual(a[0].source_event_key,b[0].source_event_key)
        self.assertNotEqual(a[0].normalized_fingerprint,b[0].normalized_fingerprint)
        with self.assertRaisesRegex(CaptureError, '^CONFLICT$'):
            replay_captures(a+b)
        with self.assertRaisesRegex(CaptureError, '^AMBIGUOUS_IDENTITY$'):
            replay_captures(a+(replace(a[0], message_id='different-id'),))

    def test_g0_13_explicit_identity_reconciliation(self):
        a,b = self.check_fixture(13)
        self.assertNotEqual(a[0].source_event_key,b[0].source_event_key)
        self.assertEqual(len(replay_captures(a+b)),2)
        mapping = fixture(13)['reconciliation']
        receipt = reconcile_conversation(a[0],b[0],mapping)
        self.assertEqual(receipt['kind'],'EXPLICIT_CONVERSATION_RECONCILIATION')
        self.assertEqual(receipt['old_capture_id'],a[0].source_event_key)
        self.assertEqual(receipt['new_capture_id'],b[0].source_event_key)
        for bad in ({}, {**mapping,'stable_id':'wrong'}):
            with self.assertRaisesRegex(CaptureError,'AMBIGUOUS_IDENTITY'):
                reconcile_conversation(a[0],b[0],bad)
        h=fixture(1)['snapshots'][0]['html']
        with self.assertRaisesRegex(CaptureError,'AMBIGUOUS_IDENTITY'):
            parse_dom(h.replace('data-c1-id="conv-01"','data-c1-id="conv-01" data-c1-temp-id="temp-01"'))

    def test_g0_14_secret_scrub_before_persistable_output(self):
        results = self.check_fixture(14)
        encoded=json.dumps([x.to_dict() for x in results[0]])
        self.assertIn('[SECRET_REDACTED]',encoded)
        self.assertNotIn('SYNTHETIC_SENTINEL_F14',encoded)
        # Durably materialize ONLY sanitized output in an isolated test directory.
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'persistable-capture.json'
            out.write_text(encoded)
            self.assertNotIn('SYNTHETIC_SENTINEL_F14',out.read_text())
        h=fixture(1)['snapshots'][0]['html']
        with self.assertRaisesRegex(CaptureError,'AMBIGUOUS_IDENTITY') as cm:
            parse_dom(h.replace('data-c1-message="m-01"','data-c1-message="sk-SYNTHETIC_F14"'))
        self.assertNotIn('SYNTHETIC_F14',str(cm.exception))
        split_markup=fixture(14)['snapshots'][0]['html'].replace('<span>key=', '<strong>key=').replace('F14</span>', 'F14</strong>')
        self.assertEqual(parse_dom(split_markup)[0].payload,'[SECRET_REDACTED]')

    def test_g0_15_hidden_dom_noise_exclusion(self):
        self.check_fixture(15)
        h=fixture(1)['snapshots'][0]['html']
        for attr in ['hidden','inert','aria-hidden="true"','style="visibility: hidden"']:
            with self.assertRaisesRegex(CaptureError,'PROFILE_DRIFT'):
                parse_dom(h.replace('<main ',f'<main {attr} '))

    def test_g0_16_profile_drift_fail_closed(self):
        self.check_fixture(16)
        profile=load_profile()
        for key in ['visible_role','drift_sanity_checks','streaming_marker']:
            changed=deepcopy(profile)
            del changed[key]
            changed['profile_fingerprint']=fingerprint({k:v for k,v in changed.items() if k!='profile_fingerprint'})
            with self.assertRaisesRegex(CaptureError,'PROFILE_DRIFT'):
                parse_dom(fixture(1)['snapshots'][0]['html'],profile=changed)

    def test_g0_17_declared_ui_variations(self):
        variants=self.check_fixture(17)
        self.assertEqual(variants,[variants[0]]*3)

    def test_g0_18_unsupported_modes(self):
        h=fixture(1)['snapshots'][0]['html']
        for mode in ['mobile','desktop-unknown','grok','gemini']:
            with self.assertRaisesRegex(CaptureError,'PROFILE_DRIFT'):
                parse_dom(h.replace('desktop-light-en',mode))
        for vendor in ['grok','gemini']:
            with self.assertRaisesRegex(CaptureError,'PROFILE_DRIFT'):
                parse_dom(h.replace('data-c1-vendor="chatgpt"',f'data-c1-vendor="{vendor}"'))

    def test_g0_19_a019_unavailable_never_false_commit(self):
        self.assertEqual(fixture(19)['future_control_condition'],'A019_UNAVAILABLE')
        # S0 must not reach the port, regardless of a hypothetical store condition.
        with patch.object(Journal,'ingest',side_effect=AssertionError('S0 must not call ingest')):
            capture=self.check_fixture(19)[0][0].to_dict()
        self.assertFalse(capture['canonical_commit'])
        self.assertEqual(capture['ingest_state'],'NOT_ATTEMPTED')
        self.assertNotIn('receipt',capture)

    def test_g0_20_contract_mismatch_fail_closed(self):
        self.check_fixture(20)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for path in CONTRACT_HASHES:
                (root/path).parent.mkdir(parents=True,exist_ok=True)
                (root/path).write_bytes((ROOT/path).read_bytes())
            verify_pins(root,BASE_SHA)
            (root/'schemas/canonical_event_v1.schema.json').write_text('{}')
            with self.assertRaisesRegex(CaptureError,'CONTRACT_CHANGE_REQUIRED'):
                verify_pins(root,BASE_SHA)

    def test_g0_21_vendor_profile_fingerprint_deterministic(self):
        a,b=load_profile(),load_profile()
        self.assertEqual(a,b)
        self.assertEqual(a['profile_fingerprint'],MANIFEST['profile_fingerprint'])
        self.assertEqual(a['profile_fingerprint'],fingerprint({k:v for k,v in a.items() if k!='profile_fingerprint'}))
        a['profile_version']='99'
        with self.assertRaisesRegex(CaptureError,'PROFILE_DRIFT'):
            validate_profile(a)

    def test_g0_22_adrs_and_synthetic_calibration(self):
        decisions=json.loads((DOCS/'s0-decisions.json').read_text())
        self.assertEqual(decisions['packaging'],'SELECT_B')
        self.assertEqual(decisions['staging_medium'],'local SQLite recovery buffer')
        self.assertFalse(decisions['staging_is_authority'])
        self.assertFalse(decisions['browser_sync'])
        self.assertEqual(decisions['implemented_stage'],'S0')
        self.assertTrue(all(v is False for v in decisions['claims'].values()))
        self.assertEqual(len(list((DOCS/'adr').glob('ADR-C1-00*.md'))),4)
        cal=MANIFEST['calibration']
        actual=calibrate_window(cal['traces'],cal['margin_ms'])
        self.assertEqual(actual,500)
        self.assertEqual(actual,decisions['candidate_T_stable_ms'])
        self.assertEqual(actual,load_profile()['terminal_evidence']['candidate_T_stable_ms'])
        with self.assertRaisesRegex(CaptureError,'INVALID_CALIBRATION'):
            calibrate_window([{'terminal_observed_ms':1,'mutation_times_ms':[1,0]}])

    def test_g0_23_proof_mapping_complete(self):
        mapping=json.loads((DOCS/'adr/proof-test-mapping.json').read_text())
        self.assertEqual([r['proof_id'] for r in mapping],[f'P{i:02}' for i in range(1,16)])
        available={name for name in dir(self) if name.startswith('test_g0_')}
        for row in mapping:
            self.assertTrue(row['current_stage_owner'])
            self.assertTrue(row['positive_test'])
            self.assertTrue(row['negative_test'])
            self.assertTrue(row['current_evaluability'])
            self.assertTrue(row['future_live_dependency'])
            for name in row['s0_executable_tests']:
                self.assertIn(name,available)
            self.assertTrue(row['s0_executable_tests'])
            self.assertTrue(row['later_test_ids'])

    def test_g0_24_forbidden_call_and_dependency_scan(self):
        self.assertEqual(scan_source(),[])
        baseline=json.loads((DOCS/'s0-baseline.json').read_text())
        for path in ['pyproject.toml','.github/workflows/test.yml']:
            self.assertEqual(hashlib.sha256((ROOT/path).read_bytes()).hexdigest(),baseline['readonly_sha256'][path])
        with patch('socket.socket',side_effect=AssertionError('network forbidden')):
            self.check_fixture(1)
            self.check_fixture(2)
        with self.assertRaisesRegex(CaptureError,'PROFILE_DRIFT'):
            parse_dom('<main>'+'x'*2_000_000+'</main>')

    def check_readonly(self, predicate):
        baseline=json.loads((DOCS/'s0-baseline.json').read_text())
        actual_paths={str(p.relative_to(ROOT)) for pattern in baseline['readonly_patterns']
                      for p in ROOT.glob(pattern) if p.is_file() and predicate(str(p.relative_to(ROOT)))}
        pinned={p:h for p,h in baseline['readonly_sha256'].items() if predicate(p)}
        self.assertTrue(pinned)
        self.assertEqual(actual_paths,set(pinned))
        for path,digest in pinned.items():
            self.assertEqual(hashlib.sha256((ROOT/path).read_bytes()).hexdigest(),digest)

    def test_g0_25_c2_unchanged(self):
        self.check_readonly(lambda p:'chatgpt_recovery' in p or p=='docs/c2-recovery-prototype.md')

    def test_g0_26_a019_contract_owned_home_unchanged(self):
        self.check_readonly(c1_a029_frozen_predicate)

    def test_g0_26a_a029_compatibility_negative_guards(self):
        expected = {
            'companion_mind/owned_home/source_pack.py',
            'companion_mind/owned_home/readonly_session.py',
            'companion_mind/owned_home/runtime.py',
            'companion_mind/owned_home/testport.py',
            'companion_mind/owned_home/shell.py',
            'companion_mind/owned_home/trace.py',
        }
        self.assertEqual(A029_P3S1_MUTABLE_OWNED_HOME, expected)
        self.assertTrue(c1_a029_frozen_predicate('companion_mind/owned_home/action_control.py'))
        self.assertTrue(c1_a029_frozen_predicate('companion_mind/owned_home/future_unapproved.py'))
        self.assertTrue(all(not c1_a029_frozen_predicate(path) for path in expected))

        # A seventh Owned Home path must still make the frozen-set check fail.
        probe = ROOT / 'companion_mind/owned_home/__a029_unapproved_probe__.py'
        self.assertFalse(probe.exists())
        try:
            probe.write_text('# synthetic compatibility guard\n')
            with self.assertRaises(AssertionError):
                self.check_readonly(c1_a029_frozen_predicate)
        finally:
            probe.unlink(missing_ok=True)

        # A non-exempt pinned Owned Home hash change must still fail.
        pinned = ROOT / 'companion_mind/owned_home/action_control.py'
        original = pinned.read_bytes()
        try:
            pinned.write_bytes(original + b'\n# synthetic compatibility hash guard\n')
            with self.assertRaises(AssertionError):
                self.check_readonly(c1_a029_frozen_predicate)
        finally:
            pinned.write_bytes(original)
        self.check_readonly(c1_a029_frozen_predicate)

    def test_g0_27_100_repeated_parse_replay_cycles(self):
        for n in range(1,21):
            f=fixture(n)
            for s in f['snapshots']:
                if 'error' in s:
                    for _ in range(100):
                        self.check_snapshot(s)
                    continue
                baseline=self.check_snapshot(s)
                unique=()
                for _ in range(100):
                    result=self.check_snapshot(s)
                    self.assertEqual(result,baseline)
                    unique=replay_captures(unique+result)
                self.assertEqual(unique,baseline)
                self.assertEqual(len(unique),len(baseline))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt',type=Path)
    args=parser.parse_args()
    class Results(unittest.TextTestResult):
        def startTest(self,test):
            if not hasattr(self,'passed'): self.passed=[]
            super().startTest(test)
        def addSuccess(self,test):
            self.passed.append(test._testMethodName)
            super().addSuccess(test)
    result=unittest.TextTestRunner(verbosity=2,resultclass=Results).run(unittest.defaultTestLoader.loadTestsFromTestCase(S0Tests))
    receipt={'work_order':'ENG-C1-A018-S0-01','status':'OFFLINE_CHECKS_PASS' if result.wasSuccessful() else 'NOT_READY',
             'independent_g0_acceptance':False,'g0_tests_run':result.testsRun,'tests_passed':result.passed,
             'failures':len(result.failures),'errors':len(result.errors),'manifest_fingerprint':MANIFEST['manifest_fingerprint'],
             'profile_fingerprint':load_profile()['profile_fingerprint'],'replay_cycles_per_snapshot':100,
             'zero_duplicate_amplification':result.wasSuccessful(),'secret_sentinel_leaks':0 if result.wasSuccessful() else 'NOT_VERIFIED',
             'forbidden_source_hits':scan_source(),'candidate_T_stable_ms':500,
             'real_dom_verified':False,'live_capture':False,'provider_calls':0,'browser_install':False,
             'claim_ceiling':'synthetic/static/offline only; acceptance remains separate'}
    if args.receipt:
        args.receipt.parent.mkdir(parents=True,exist_ok=True)
        args.receipt.write_text(json.dumps(receipt,indent=2)+'\n')
    sys.exit(0 if result.wasSuccessful() else 1)
