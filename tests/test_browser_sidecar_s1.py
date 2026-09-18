"""S1 executable evidence, synthetic only. Full regression remains the Test CI.

No test here installs an extension or captures a live page. G1 family labels are
not independent acceptance; a developer pass returns READY_FOR_G1_REVIEW only.
"""
import ast
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import itertools
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion_mind.browser_sidecar.identity import IdentityReducer
from companion_mind.browser_sidecar.normalize import Attachment, Capture, CaptureError, fingerprint
from companion_mind.browser_sidecar.parser import parse_dom

CORPUS = ROOT / "tests/fixtures/c1_chatgpt_web_v0_1"
FIXTURE_SHA256 = {
    "F08": "18e793eef2cdaa7aa931f09449437241dfe372b0701b2d785d2c35ca1a68ffac",
    "F09": "6926a2c47d3e1d6ed0c7d3170807adc6b1d1d317884314e3a621f8ceacf6a65b",
    "F10": "92aac30eeba18503bdd96998cee3993ddf4636d4afe23047f41514387092d2da",
    "F11": "cd93cd90e4e03a731737f2967c703ecb2fecf7c70da53dc49d85d40227e632f4",
    "F12": "8fafe98616860dde7704f47dcc02a1931687e810addbce609c89b4357ec3bfb7",
    "F13": "2a502235eaf24e31cd378301a717909c6956fab34eed6bc409017d28339287d5",
}
BASE_BLOBS = {
    "companion_mind/browser_sidecar/normalize.py": "2e4baf00d63ad67d26b09adbd977f565ff44462a",
    "companion_mind/browser_sidecar/parser.py": "2090ae074af69507f380f9c8cd2f702bc3a3fed2",
    "companion_mind/browser_sidecar/vendor_profile.py": "59d5f9a0d9672e30919a62ff3416327c7c033891",
    "companion_mind/browser_sidecar/profiles/chatgpt_web_v0_1.json": "7dd4542706ccac907e3f341c50354831ef34f599",
}
PREFLIGHT_BLOB = "3ef8589eb72478de7b0ea1a960a6b816a7b249d4"
EXACT_ALLOWANCE = '            if path == "tests/test_browser_sidecar_s1.py":\n                return True\n'


def git_blob(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def fixture(name):
    data = (CORPUS / (name + ".json")).read_bytes()
    if hashlib.sha256(data).hexdigest() != FIXTURE_SHA256[name]:
        raise AssertionError("FIXTURE_DRIFT")
    return json.loads(data)


def snapshots(name):
    return [parse_dom(s["html"]) for s in fixture(name)["snapshots"]]


def basic():
    return snapshots("F09")[0][0]


def mapping():
    return dict(fixture("F13")["reconciliation"])


def forbid_surface(source):
    tree = ast.parse(source)
    allowed_imports = {"dataclasses", "re", "normalize", "vendor_profile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(n.name not in allowed_imports for n in node.names):
            raise AssertionError("FORBIDDEN_IMPORT")
        if isinstance(node, ast.ImportFrom) and node.module not in allowed_imports:
            raise AssertionError("FORBIDDEN_IMPORT")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name in {"open", "exec", "eval", "compile", "__import__", "connect", "request", "urlopen",
                        "write", "write_text", "write_bytes", "read_text", "read_bytes", "load_profile"}:
                raise AssertionError("FORBIDDEN_CALL")


class S1IdentityTests(unittest.TestCase):
    def reject(self, reducer, code, operation):
        before = reducer.snapshot()
        with self.assertRaises(CaptureError) as error:
            operation()
        self.assertEqual(str(error.exception), code)
        self.assertEqual(before, reducer.snapshot(), "FAILED_OPERATION_MUTATED_STATE")

    def test_g1_01_identity_and_scope_separation(self):
        c = basic()
        r = IdentityReducer()
        self.assertEqual(r.observe([c]).status, "APPLIED")
        self.assertEqual(r.observe([c]).status, "IDEMPOTENT")
        r.observe([replace(c, message_id="other-message", source_position=2), replace(c, scope_alias="other-scope")])
        self.assertEqual(r.snapshot()["member_count"], 3)
        for field, value, code in (("profile_id", "OtherVendor", "PROFILE_DRIFT"),
                                   ("scope_alias", "", "AMBIGUOUS_IDENTITY"),
                                   ("source_position", True, "AMBIGUOUS_IDENTITY"),
                                   ("source_position", -1, "AMBIGUOUS_IDENTITY")):
            self.reject(r, code, lambda: r.observe([replace(c, **{field: value})]))

    def test_g1_01_forged_hash_subclass_and_missing_fields_rejected(self):
        c, r = basic(), IdentityReducer()
        serialized = c.to_dict() | {"normalized_fingerprint": "0" * 64}
        self.reject(r, "INVALID_CAPTURE", lambda: r.observe([serialized]))
        class Forged(Capture):
            @property
            def normalized_fingerprint(self):
                raise AssertionError("CALLER_OVERRIDE_INVOKED")
        self.reject(r, "INVALID_CAPTURE", lambda: r.observe([Forged(**asdict(c))]))
        self.reject(r, "INVALID_CAPTURE", lambda: r.observe([object.__new__(Capture)]))
        injected = replace(c)
        object.__setattr__(injected, "verified", True)
        self.reject(r, "INVALID_CAPTURE", lambda: r.observe([injected]))
        self.reject(r, "INVALID_CAPTURE", lambda: r.observe(iter([c])))

    def test_g1_01_accepted_values_are_detached_from_caller(self):
        c = basic()
        a = Attachment("a", "safe.txt", "text/plain", "message:m-01:attachment:a", "REFERENCE_ONLY")
        c = replace(c, attachments=(a,))
        r = IdentityReducer()
        r.observe([c])
        before = r.snapshot()
        c.__dict__["payload"] = "caller-mutated synthetic text"
        a.__dict__["filename"] = "caller-mutated.txt"
        self.assertEqual(r.snapshot(), before)
        (t,), (s,) = snapshots("F13")
        r2 = IdentityReducer()
        r2.reconcile([t], [s], mapping())
        before = r2.snapshot()
        t.__dict__["message_id"] = "caller-mutated"
        s.__dict__["payload"] = "caller-mutated"
        self.assertEqual(r2.snapshot(), before)

    def test_g1_02_normalization_fingerprint_fields_preserved(self):
        a, b = snapshots("F11")
        r = IdentityReducer()
        r.observe(a)
        self.assertEqual(r.observe(b).status, "IDEMPOTENT")
        c = a[0]
        changes = [{"role": "assistant"}, {"source_position": 2},
                   {"attachments": (Attachment("att", "a.txt", "text/plain",
                                              "message:m-01:attachment:att", "REFERENCE_ONLY"),)}]
        for change in changes:
            self.reject(r, "CONFLICT", lambda: r.observe([replace(c, **change)]))
        self.reject(r, "PROFILE_DRIFT", lambda: r.observe([replace(c, profile_version="0.2")]))
        assistant = replace(c, role="assistant")
        other = IdentityReducer()
        other.observe([assistant])
        self.reject(other, "CONFLICT", lambda: other.observe([replace(assistant, terminal_observation="partial")]))
        self.reject(other, "UNSAFE_CAPTURE", lambda: other.observe([replace(assistant, redaction_state="redacted")]))

    def test_g1_03_virtualization_reload_and_missing_identity(self):
        r = IdentityReducer()
        for _ in range(3):
            for name in ("F08", "F09"):
                for captures in snapshots(name):
                    r.observe(captures)
        self.assertEqual(r.snapshot()["member_count"], 1)
        self.reject(r, "AMBIGUOUS_IDENTITY", lambda: r.observe([replace(basic(), message_id="")]))

    def test_g1_04_tabs_order_and_scope(self):
        observations = snapshots("F10") + snapshots("F11")
        states = []
        for ordered in (observations, list(reversed(observations))):
            r = IdentityReducer()
            for captures in ordered:
                r.observe(captures)
            states.append(r.snapshot())
        self.assertEqual(states[0], states[1])
        r.observe([replace(basic(), scope_alias="other-account")])
        self.assertEqual(r.snapshot()["member_count"], 2)
        self.reject(r, "AMBIGUOUS_IDENTITY", lambda: r.observe([replace(basic(), message_id="another")]))

    def test_g1_05_conflict_batch_is_atomic(self):
        first, second = snapshots("F12")
        r = IdentityReducer()
        r.observe(first)
        new = replace(first[0], message_id="new-message", source_position=2)
        inputs = [new, second[0]]
        saved = deepcopy(inputs)
        self.reject(r, "CONFLICT", lambda: r.observe(inputs))
        self.assertTrue(inputs == saved, "INPUT_MUTATED")
        self.assertEqual(r.observe(first).status, "IDEMPOTENT")
        r.observe([new])
        self.assertEqual(r.snapshot()["member_count"], 2)

    def test_g1_06_reconciliation_arrival_orders_and_detachment(self):
        temporary, stable = snapshots("F13")
        expected = None
        for order in ((temporary, stable), (stable, temporary), (temporary,), (stable,), ()):
            r = IdentityReducer()
            for captures in order:
                r.observe(captures)
            original = deepcopy((temporary, stable))
            r.reconcile(temporary, stable, mapping())
            before = r.snapshot()
            self.assertEqual(before["member_count"], 1)
            self.assertEqual(len(before["mapping_evidence"]), 1)
            self.assertEqual(r.observe(temporary + stable).status, "IDEMPOTENT")
            self.assertEqual(r.reconcile(temporary, stable, mapping()).status, "IDEMPOTENT")
            self.assertEqual(r.snapshot(), before)
            self.assertTrue(original == (temporary, stable), "INPUT_MUTATED")
            if expected is not None:
                self.assertEqual(expected, before)
            expected = before
            detached = r.snapshot()
            detached["observations"][0]["payload"] = "external mutation"
            detached["members"][0]["source_capture_ids"].append("external")
            self.assertEqual(r.snapshot(), before)

    def test_g1_06_whole_set_and_stable_only_members(self):
        (t,), (s,) = snapshots("F13")
        t2, s2 = replace(t, message_id="m-02", source_position=2), replace(s, message_id="m-02", source_position=2)
        s3 = replace(s, message_id="m-03", source_position=3)
        r = IdentityReducer()
        r.observe([t, t2, s3])
        self.reject(r, "AMBIGUOUS_IDENTITY", lambda: r.reconcile([t], [s], mapping()))
        r.reconcile([t, t2], [s, s2], mapping())
        state = r.snapshot()
        self.assertEqual((state["member_count"], len(state["observations"]), len(state["positions"])), (3, 5, 3))
        self.assertEqual(len(state["mapping_evidence"]), 2)
        self.assertEqual(r.observe([t2, s2, t, s]).status, "IDEMPOTENT")

    def test_g1_07_mapping_rejections_are_atomic(self):
        (t,), (s,) = snapshots("F13")
        r = IdentityReducer()
        r.observe([t, s])
        for bad in ({}, mapping() | {"extra": "x"}, mapping() | {"scope_alias": "wrong"},
                    mapping() | {"stable_id": "missing"}):
            self.reject(r, "AMBIGUOUS_IDENTITY", lambda: r.reconcile([t], [s], bad))
        self.reject(r, "AMBIGUOUS_IDENTITY", lambda: r.reconcile([s], [s], mapping()))
        self.reject(r, "AMBIGUOUS_IDENTITY", lambda: r.reconcile([t], [t], mapping()))
        self.reject(r, "PROFILE_DRIFT", lambda: r.reconcile([t], [replace(s, profile_id="other")], mapping()))
        self.reject(r, "AMBIGUOUS_IDENTITY", lambda: r.reconcile([t], [replace(s, scope_alias="other")], mapping()))
        r.reconcile([t], [s], mapping())
        self.reject(r, "AMBIGUOUS_IDENTITY", lambda: r.reconcile(
            [t], [replace(s, conversation_id="stable-02")], mapping() | {"stable_id": "stable-02"}))
        self.reject(r, "CONFLICT", lambda: r.observe([replace(t, payload="changed")]))

    def test_g1_07_multiple_temporary_proofs_and_late_position_conflict(self):
        (t,), (s,) = snapshots("F13")
        r = IdentityReducer()
        r.reconcile([t], [s], mapping())
        t2 = replace(t, conversation_id="temp-02")
        r.reconcile([t2], [s], mapping() | {"temporary_id": "temp-02"})
        self.assertEqual((r.snapshot()["member_count"], len(r.snapshot()["aliases"])), (1, 2))
        r2 = IdentityReducer()
        r2.observe([t, replace(s, message_id="different-at-same-position")])
        self.reject(r2, "AMBIGUOUS_IDENTITY", lambda: r2.reconcile([t], [s], mapping()))
        r3 = IdentityReducer()
        bad_s = replace(s, payload="different")
        r3.observe([t, bad_s])
        self.reject(r3, "AMBIGUOUS_IDENTITY", lambda: r3.reconcile([t], [bad_s], mapping()))

    def test_g1_08_position_role_and_unknown(self):
        c, r = basic(), IdentityReducer()
        r.observe([c])
        self.reject(r, "AMBIGUOUS_IDENTITY", lambda: r.observe([replace(c, message_id="other-message")]))
        self.reject(r, "AMBIGUOUS_ROLE", lambda: r.observe([replace(c, role="unknown")]))
        self.reject(r, "AMBIGUOUS_IDENTITY", lambda: r.observe([replace(c, conversation_kind="unknown")]))
        other = replace(c, message_id="other-message", source_position=2)
        states = []
        for ordered in itertools.permutations([c, other]):
            fresh = IdentityReducer()
            for capture in ordered:
                fresh.observe([capture])
            states.append(fresh.snapshot())
        self.assertEqual(states[0], states[1])
        self.assertFalse(states[0]["canonical_commit"])
        self.assertEqual(states[0]["ingest_state"], "NOT_ATTEMPTED")

    def test_g1_09_streaming_stabilizing_hold_and_terminal(self):
        c = replace(basic(), role="assistant")
        r = IdentityReducer()
        empty = r.snapshot()
        for state in ("STREAMING", "STABILIZING"):
            for text in ("first fragment", "second fragment"):
                hold = replace(c, control_state=state, terminal_observation=None, payload=text)
                self.assertEqual(r.observe([hold]).status, "HOLD")
                self.assertEqual(r.snapshot(), empty)
        self.assertEqual(r.observe([c]).member_count, 1)
        self.assertEqual(r.observe([c]).status, "IDEMPOTENT")
        self.reject(r, "AMBIGUOUS_TERMINAL", lambda: r.observe([replace(c, control_state="STREAMING")]))
        self.reject(r, "AMBIGUOUS_TERMINAL", lambda: r.observe([replace(c, terminal_observation=None)]))

    def test_g1_09_mixed_hold_withholds_entire_batch(self):
        c = basic()
        streaming = replace(c, role="assistant", source_position=2, message_id="m-02",
                            control_state="STREAMING", terminal_observation=None)
        r = IdentityReducer()
        self.assertEqual(r.observe([c, streaming]).status, "HOLD")
        self.assertEqual(r.snapshot()["member_count"], 0)
        r.observe([c])
        self.reject(r, "CONFLICT", lambda: r.observe([replace(c, payload="different"), streaming]))
        (t,), (s,) = snapshots("F13")
        th = replace(t, role="assistant", control_state="STREAMING", terminal_observation=None)
        sh = replace(s, role="assistant", control_state="STREAMING", terminal_observation=None)
        before = r.snapshot()
        self.assertEqual(r.reconcile([th], [sh], mapping()).status, "HOLD")
        self.assertEqual(before, r.snapshot())

    def test_g1_10_secret_input_and_unsafe_references_rejected(self):
        c, r = basic(), IdentityReducer()
        sentinel = "SYNTHETIC-" + "ONLY-S1-CANARY"
        secret = "password=" + sentinel
        self.reject(r, "UNSAFE_CAPTURE", lambda: r.observe([replace(c, payload=secret)]))
        self.reject(r, "UNSAFE_CAPTURE", lambda: r.observe([replace(c, payload="https://example.invalid/x?q=" + sentinel)]))
        a = Attachment("a", "safe.txt", "text/plain", "message:m-01:attachment:a", "REFERENCE_ONLY")
        for bad in (replace(a, source_ref="https://example.invalid/?q=" + sentinel),
                    replace(a, filename=secret), replace(a, url_omitted=False)):
            self.reject(r, "UNSAFE_CAPTURE", lambda: r.observe([replace(c, attachments=(bad,))]))
        self.assertNotIn(sentinel, json.dumps(r.snapshot()))
        clean = replace(c, payload="[SECRET_REDACTED]", redaction_state="redacted")
        r.observe([clean])
        self.assertNotIn(sentinel, json.dumps(r.snapshot()))

    def test_g1_10_split_markup_and_forbidden_surface(self):
        html = fixture("F09")["snapshots"][0]["html"]
        sentinel = "SYNTHETIC-" + "ONLY-S1-CANARY"
        html = html.replace("Hello world", "pass<strong>word</strong>=" + sentinel)
        r = IdentityReducer()
        r.observe(parse_dom(html))
        serialized = json.dumps(r.snapshot())
        self.assertNotIn(sentinel, serialized)
        self.assertIn("[SECRET_REDACTED]", serialized)
        source = (ROOT / "companion_mind/browser_sidecar/identity.py").read_text(encoding="utf-8")
        forbid_surface(source)
        with self.assertRaisesRegex(AssertionError, "FORBIDDEN_IMPORT"):
            forbid_surface(source + "\nimport socket\n")
        with self.assertRaisesRegex(AssertionError, "FORBIDDEN_CALL"):
            forbid_surface(source + '\nopen("not-a-real-path", "w")\n')
        self.assertFalse(r.observe([]).to_dict()["authority"])

    def test_g1_11_hundred_combined_replay_cycles(self):
        r, conflict = IdentityReducer(), IdentityReducer()
        (t,), (s,) = snapshots("F13")
        first, second = snapshots("F12")
        conflict.observe(first)
        groups = [captures for name in ("F08", "F09", "F10", "F11") for captures in snapshots(name)]
        for captures in groups:
            r.observe(captures)
        r.reconcile([t], [s], mapping())
        before, before_error = r.snapshot(), conflict.snapshot()
        for cycle in range(100):
            for captures in (groups if cycle % 2 else list(reversed(groups))):
                r.observe(captures)
            r.observe([t, s])
            r.reconcile([t], [s], mapping())
            self.reject(conflict, "CONFLICT", lambda: conflict.observe(second))
            self.assertEqual(r.snapshot(), before)
            self.assertEqual(conflict.snapshot(), before_error)
        self.assertEqual(before["member_count"], 2)
        print("S1_REPLAY_RECEIPT " + json.dumps({"cycles": 100, "duplicate_amplification": 0,
              "state_sha256": fingerprint(before), "conflict_state_sha256": fingerprint(before_error)}, sort_keys=True))

    def test_g1_12_exact_source_and_preflight_boundary(self):
        for path, expected in BASE_BLOBS.items():
            raw = (ROOT / path).read_bytes()
            self.assertEqual(git_blob(raw), expected, "PROTECTED_SOURCE_DRIFT")
            self.assertNotEqual(git_blob(raw + b"\n# mutation\n"), expected)
        raw = (ROOT / "tests/test_owned_home_slice1_conformance.py").read_text(encoding="utf-8")
        self.assertEqual(raw.count(EXACT_ALLOWANCE), 1)
        self.assertIn("        def permitted(path):\n" + EXACT_ALLOWANCE, raw)
        restored = raw.replace(EXACT_ALLOWANCE, "", 1).encode("utf-8")
        self.assertEqual(git_blob(restored), PREFLIGHT_BLOB, "NON_ALLOWED_PREFLIGHT_CHANGE")
        self.assertIn("60b0dc12c68bddf6edb508345c6c6fbccf48cdf7", raw)
        baseline = json.loads((ROOT / "docs/browser_sidecar/s1-baseline.json").read_text())
        self.assertEqual(baseline["base_sha"], "709c590387f745b8716537f11cc40cd469001753")
        self.assertEqual(baseline["base_tree"], "19cdee17eadf4c7aa16709792e33115812d08d6f")
        self.assertEqual(baseline["readonly_git_blobs"], BASE_BLOBS)
        self.assertEqual(len(baseline["add_paths"]), 5)
        self.assertEqual(baseline["modify_paths"], ["tests/test_owned_home_slice1_conformance.py"])
        proof = json.loads((ROOT / "docs/browser_sidecar/s1-proof-test-mapping.json").read_text())
        self.assertEqual(set(proof["g1_families"]), {f"G1-{i:02d}" for i in range(1, 13)})
        self.assertEqual(set(proof["proofs"]), {f"P{i:02d}" for i in range(1, 16)})
        listed = {name for row in proof["g1_families"].values() for name in row["test_ids"]}
        actual = {"S1IdentityTests." + n for n in unittest.defaultTestLoader.getTestCaseNames(type(self))}
        self.assertEqual(listed, actual)


if __name__ == "__main__":
    unittest.main(verbosity=2)
