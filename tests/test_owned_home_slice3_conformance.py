"""WO-A1-A029-P2S3-01: public JSON/CLI/loopback conformance only.

Run: python tests/test_owned_home_slice3_conformance.py --receipt
The CLI remains python -m companion_mind.owned_home.testport --store DIR.
Synthetic setup optionally adds model_profiles=[ModelProfile public fields].
Operations: model_registry; model_lookup(profile_key, profile_version);
model_preview(turn); model_validate_spec(turn, spec); model_turn(turn,
expected_spec?, resume?). A model turn extends ContextTurn with model_intent
and model_script=COMPLETE|PARTIAL|FAILED|TIMEOUT|UNKNOWN. Missing intent fields
use the public synthetic defaults. Preferred key/version are exact; null/null
requests deterministic selection. fallback_policy is STOP by default, or
PRE_CALL_COMPATIBLE. required/allowed_capabilities use structured_output,
tool_calls, media, files. Capabilities authorize no real tools or file access.
Spec validation and preview invoke nothing. The six persisted /models browser
fields are session/topic/request/turn_no and allowlisted profile key/version.
Five-way model_result is separate from A019's text durability status. Historical
terminal attempts are immutable; resume never retries a terminal UNKNOWN.
Interrupted invocation count is UNKNOWN with upper bound 1, never invented.

No internal DB, cache, registry object or private content is a behavioral oracle.
Repository bytes/Git objects are inspected only for the required scope gate.
"""
import ast
from copy import deepcopy
from http.client import HTTPConnection
import itertools
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from companion_mind.owned_home.testport import execute
from companion_mind.owned_home.shell import make_server
from test_owned_home_slice1_conformance import BROWSER_HARNESS
from test_owned_home_slice2_conformance import request as context_request, fixture, encode, digest

BASE_SHA = "0bb16b98aa9dbd5adacce7da2285f736793e7e04"
BASE_TREE = "31bc1f2ea879095e3797f9342884046a2a694374"
PRIOR = {"TS1": "5b8dd9100d3ec7d0370a7ac489499b2a10677d0db0a07ce80575445a32678be1",
         "TS2": "05ac94539f2b7be4829e3a3405b9f053f21f1c2ba2e3fcff4fc81bb4293f5999",
         "A019": "d20d06a356051f71972ef1f5aa34d1dc72eddb8f70b74773ffb0a535ed1cfa6d"}
MATRIX = {}
PROFILE_FIELDS = ("provider_key", "model_key", "profile_key", "profile_version", "context_capacity",
                  "output_reserve", "envelope_reserve", "estimator_id", "estimator_version", "structured_output",
                  "tool_calls", "media", "files", "availability", "health", "cost_rank", "latency_ms", "synthetic", "public_safe")


def request(number=1, topic="A", profile="synthetic-small", **kwargs):
    r = context_request(number, topic, **kwargs)
    r["op"] = "model_turn"
    r["turn"].update(model_intent={"preferred_profile_key": profile,
                                   "preferred_profile_version": "v1" if profile else None},
                      model_script="COMPLETE")
    return r


def operation(req, op, **fields):
    return {k: deepcopy(v) for k, v in req.items() if k in (
        "contract_version", "scope", "fixtures", "grants", "model_profiles")} | {"op": op, **fields}


def process(store, req, fault=None):
    command = [sys.executable, "-m", "companion_mind.owned_home.testport", "--store", str(store)]
    if fault:
        command += ["--fault", fault]
    return subprocess.run(command, input=encode(req), text=True, capture_output=True, cwd=ROOT, timeout=25)


class Slice3Conformance(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.root / "home"

    def call(self, req, store=None):
        return execute(store or self.store, deepcopy(req))

    def child(self, req, store=None):
        child = process(store or self.store, req)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        return json.loads(child.stdout)["result"]

    def rejected(self, req, store=None, expected=None):
        child = process(store or self.store, req)
        self.assertEqual(child.returncode, 2, child.stdout + child.stderr)
        error = json.loads(child.stdout)["error"]
        if expected:
            self.assertEqual(error, expected)
        return error

    def profiles(self):
        registry = self.call(operation(request(), "model_registry"))
        return [{k: p[k] for k in PROFILE_FIELDS} for p in registry["profiles"]]

    def passed(self, case, **facts):
        MATRIX[case] = {"status": "PASS", **facts}

    def test_ts3_01_registry_determinism(self):
        profiles, projections = self.profiles(), []
        for ordering in itertools.permutations(profiles):
            req = operation(request(), "model_registry") | {"model_profiles": list(ordering)}
            projections.append(self.call(req))
        self.assertTrue(all(p == projections[0] for p in projections))
        registry = projections[0]
        self.assertEqual(registry["registry_fingerprint"], digest(registry["profiles"]))
        for p in registry["profiles"]:
            self.assertEqual(p["profile_fingerprint"], digest({k: p[k] for k in PROFILE_FIELDS}))
        bad = operation(request(), "model_registry") | {"model_profiles": [profiles[0], profiles[0]]}
        self.rejected(bad, self.root / "duplicate", "DUPLICATE_PROFILE_IDENTITY")
        self.assertFalse((self.root / "duplicate").exists())
        self.assertEqual(len({p["usable_input_capacity"] for p in registry["profiles"]}), 3)
        self.assertFalse(registry["estimator"]["vendor_tokenizer_parity"])
        self.passed("TS3-01", permutations=6, duplicates="DENY", registry_fingerprint=registry["registry_fingerprint"],
                    profiles=[{k: p[k] for k in ("profile_key", "profile_version", "profile_fingerprint")} for p in registry["profiles"]])

    def test_ts3_02_exact_profile_version(self):
        r = self.call(request())
        self.assertEqual(r["model_result"]["profile_ref"], {"profile_key": "synthetic-small", "profile_version": "v1"})
        for key, version in (("synthetic-small", "v0"), ("synthetic-missing", "v1")):
            q = request()
            q["turn"]["model_intent"].update(preferred_profile_key=key, preferred_profile_version=version,
                                             fallback_policy="PRE_CALL_COMPATIBLE")
            stopped = self.call(q, self.root / key)
            self.assertEqual(stopped["provider_invocations"], 0)
            self.assertEqual(stopped["stop_reason"], "NO_COMPATIBLE_PROFILE")
            self.assertEqual(stopped["model_trace"]["selection_reason"], "EXACT_PROFILE_UNRESOLVED_OR_NOT_ALLOWED")
        profiles = self.profiles()
        v2 = next(deepcopy(p) for p in profiles if p["profile_key"] == "synthetic-small")
        v2.update(profile_version="v2", context_capacity=8192)
        q = request(); q["model_profiles"] = profiles + [v2]
        q["turn"]["model_intent"].update(preferred_profile_version="v2",
            allowed_profiles=[{"profile_key": "synthetic-small", "profile_version": "v2"}])
        selected = self.call(q, self.root / "version2")
        self.assertEqual(selected["model_result"]["profile_ref"]["profile_version"], "v2")
        self.assertEqual(selected["projection"]["context"]["budget"]["limit"], 8192-256-128)
        lookup = self.call(operation(q, "model_lookup", profile_key="synthetic-small", profile_version="v3"))
        self.assertEqual(lookup, {"status": "EXACT_PROFILE_UNRESOLVED", "profile": None, "provider_invocations": 0})
        self.passed("TS3-02", exact_versions=["v1", "v2"], unresolved="STOP", wrong_profile_returns=0)

    def test_ts3_03_capability_filtering(self):
        for cap in ("structured_output", "tool_calls", "media", "files"):
            q = request(); q["turn"]["model_intent"]["required_capabilities"] = [cap]
            denied = self.call(q, self.root / cap)
            self.assertEqual(denied["provider_invocations"], 0)
            self.assertEqual(denied["stop_reason"], "NO_COMPATIBLE_PROFILE")
            self.assertIsNone(denied["model_result"])
            small = next(c for c in denied["model_trace"]["candidate_profile_refs"] if c["profile_key"] == "synthetic-small")
            self.assertFalse(small["checks"]["capabilities"][cap])
        q = request(profile="synthetic-capable")
        q["turn"]["model_intent"].update(required_capabilities=["tool_calls", "files", "media"], response_format="json")
        allowed = self.call(q)
        self.assertEqual(json.loads(allowed["visible_reply"]), {"outcome": "COMPLETE", "synthetic": True})
        self.assertEqual(allowed["model_result"]["tool_action_intents"], [])
        self.assertEqual(allowed["counters"]["external_side_effects"], 0)
        q["turn"]["model_intent"]["allowed_capabilities"] = ["files"]
        self.rejected(q, self.root / "not-allowed", "CAPABILITY_NOT_ALLOWED")
        q = request(); q["turn"]["model_intent"]["response_format"] = "json"
        self.assertEqual(self.call(q, self.root / "format")["provider_invocations"], 0)
        self.passed("TS3-03", required_capabilities=4, implicit_json_requirement=True, mismatch_invocations=0,
                    silent_downgrades=0, real_tool_execution=0)

    def test_ts3_04_dynamic_exact_budget(self):
        fixtures = [fixture(text="汉字 SYNTHETIC A"), fixture("beta", text="SYNTHETIC B " * 450)]
        q = request(fixtures=fixtures, needs=[{"source_id": "alpha"}, {"source_id": "beta"}])
        small = self.call(q)
        q["turn"]["model_intent"]["preferred_profile_key"] = "synthetic-large"
        large = self.call(q, self.root / "large")
        packs = [x["projection"]["context"] for x in (small, large)]
        self.assertEqual([p["budget"]["limit"] for p in packs], [3712, 16384])
        self.assertEqual([len(p["included"]) for p in packs], [1, 2])
        for pack in packs:
            payload = {"topic_id": "A", "user_text": q["turn"]["text"], "recent_exact_turns": [],
                       "evidence": [{"ref": ref, "text": next(f["text"] for f in fixtures if f["source_id"] == ref["source_id"])} for ref in pack["included"]]}
            self.assertEqual(pack["input_fingerprint"], digest(payload))
            self.assertEqual(pack["budget"]["included_bytes"], len(encode(payload).encode()))
            self.assertLessEqual(pack["budget"]["included_bytes"], pack["budget"]["limit"])
            self.assertEqual(pack["budget"]["silent_truncations"], 0)
            b = pack["model_budget"]
            self.assertEqual(b["effective_input_budget"], min(b["requested_budget"], b["context_capacity"]-b["output_reserve"]-b["envelope_reserve"]))
        self.assertEqual({x["action"] for x in packs[0]["ledger"]}, {"INCLUDE", "OMIT"})
        q = request(); q["turn"]["text"] = "汉" * 1500
        stopped = self.call(q, self.root / "too-small")
        self.assertEqual(stopped["stop_reason"], "CONTEXT_BUDGET_EXCEEDED")
        self.assertEqual(stopped["provider_invocations"], 0)
        self.assertEqual(stopped["projection"]["context"]["input_fingerprint"], digest(None))
        self.passed("TS3-04", small_budget=3712, large_budget=16384, exact_utf8_bytes=True, atomic_omissions=True, silent_truncations=0)

    def test_ts3_05_pre_call_fallback(self):
        profiles = self.profiles()
        for p in profiles:
            if p["profile_key"] == "synthetic-small":
                p["availability"] = "UNAVAILABLE"
        q = request(); q["model_profiles"] = profiles
        q["turn"]["model_intent"].update(fallback_policy="PRE_CALL_COMPATIBLE", required_capabilities=["tool_calls"])
        preview = self.call(q | {"op": "model_preview"})
        self.assertEqual(preview["provider_invocations"], 0)
        self.assertEqual(self.call(operation(q, "safe_export"))["events"], [])
        reverse = deepcopy(q); reverse["model_profiles"].reverse()
        self.assertEqual(self.call(reverse | {"op": "model_preview"})["selection"], preview["selection"])
        selected = self.call(q | {"expected_spec": preview["call_spec"]})
        self.assertEqual(selected["provider_invocations"], 1)
        self.assertEqual(selected["model_trace"]["selected_profile"]["profile_key"], "synthetic-capable")
        self.assertEqual(selected["model_trace"]["selection_reason"], "PRE_CALL_COMPATIBLE_FALLBACK")
        order = selected["execution_order"]
        self.assertLess(order.index("MODEL_PRE_CALL_SELECTION"), order.index("PROVIDER_STUB_INVOKED"))
        q = request(profile=None); q["turn"]["model_intent"]["minimum_input_capacity"] = 20000
        q["turn"]["budget_bytes"] = 32768
        selected = self.call(q, self.root / "capacity")
        self.assertEqual(selected["model_trace"]["selected_profile"]["profile_key"], "synthetic-large")
        q["turn"]["model_intent"]["max_cost_rank"] = 1
        self.assertEqual(self.call(q, self.root / "cost")["provider_invocations"], 0)
        self.passed("TS3-05", selection_invocations=0, fallback_invocations=1, selected="synthetic-capable/v1",
                    deterministic_order=True, capacity_and_cost_filtered=True)

    def test_ts3_06_no_compatible_keeps_user(self):
        profiles = self.profiles()
        for p in profiles:
            p["health"] = "UNHEALTHY"
        q = request(profile=None); q["model_profiles"] = profiles
        r = self.call(q)
        self.assertEqual((r["stop_reason"], r["provider_invocations"], r["terminal_count"]), ("NO_COMPATIBLE_PROFILE", 0, 1))
        events = self.call(operation(q, "safe_export"))["events"]
        self.assertEqual([e["actor_role"] for e in events], ["user", "assistant"])
        self.assertEqual(events[0]["event_id"], r["user_event_id"])
        self.assertEqual(events[0]["receipt"]["durability"], "SQLITE_WAL_FULL")
        self.assertLess(r["receipts"]["user"]["commit_generation"], r["receipts"]["assistant"]["commit_generation"])
        for _ in range(3):
            replay = self.call(operation(q, "resume", request_id="req-1"))
            self.assertEqual(replay["provider_invocations"], 0)
            self.assertEqual(replay["model_trace"]["provider_invocation_count"], 0)
            self.assertEqual(replay["terminal_count"], 1)
        self.passed("TS3-06", no_compatible="STOP", provider_invocations=0, user_durable=True, terminal_before_display=True)

    def test_ts3_07_spec_binding_invalidation(self):
        q = request(profile="synthetic-capable")
        preview = self.call(q | {"op": "model_preview"}); spec = preview["call_spec"]
        self.assertTrue(self.call(q | {"op": "model_validate_spec", "spec": spec})["valid"])
        changes = [("request_id", "different-request"), ("turn_id", "different-turn"), ("session_id", "different-session"),
                   ("topic_id", "different-topic"), ("premise_id", "task-v2"), ("budget_bytes", 8000)]
        for field, value in changes:
            changed = deepcopy(q); changed["turn"][field] = value
            check = self.call(changed | {"op": "model_validate_spec", "spec": spec})
            self.assertFalse(check["valid"], field); self.assertEqual(check["provider_invocations"], 0)
        for field, value in (("required_capabilities", ["tool_calls"]), ("allowed_capabilities", ["files"]),
                             ("output_budget", 256), ("response_format", "json"), ("timeout_ms", 2)):
            changed = deepcopy(q); changed["turn"]["model_intent"][field] = value
            self.assertFalse(self.call(changed | {"op": "model_validate_spec", "spec": spec})["valid"], field)
        changed = deepcopy(q); changed["fixtures"][0]["text"] += " NEW AUTHORITY REVISION"
        self.assertFalse(self.call(changed | {"op": "model_validate_spec", "spec": spec})["valid"])
        profiles = self.profiles()
        for p in profiles:
            if p["profile_key"] == "synthetic-capable":
                p["profile_version"] = "v2"
        changed = q | {"model_profiles": profiles}
        self.assertFalse(self.call(changed | {"op": "model_validate_spec", "spec": spec})["valid"])
        for field in ("context_fingerprint", "profile_fingerprint", "estimator_fingerprint", "spec_fingerprint"):
            stale = deepcopy(spec); stale[field] = "0" * 64
            self.assertFalse(self.call(q | {"op": "model_validate_spec", "spec": stale})["valid"])
        stale = deepcopy(spec); stale["identity"]["attempt_id"] = "changed-attempt"
        self.assertFalse(self.call(q | {"op": "model_validate_spec", "spec": stale})["valid"])
        stopped = self.call(q | {"expected_spec": stale})
        self.assertEqual((stopped["stop_reason"], stopped["provider_invocations"]), ("MODEL_SPEC_INVALID", 0))
        self.passed("TS3-07", profile_version=True, context=True, task_turn_request_attempt=True,
                    capabilities=True, budget_format_timeout=True, stale_spec_invocations=0)

    def test_ts3_08_five_outcomes(self):
        statuses = {}
        for outcome in ("COMPLETE", "PARTIAL", "FAILED", "TIMEOUT", "UNKNOWN"):
            q = request(); q["turn"]["model_script"] = outcome
            r = self.call(q, self.root / outcome)
            model = r["model_result"]
            self.assertEqual(model["outcome"], outcome)
            self.assertEqual(r["model_trace"]["terminal_model_outcome"], outcome)
            self.assertEqual(model["result_fingerprint"], digest({k: v for k, v in model.items() if k != "result_fingerprint"}))
            self.assertEqual(model["candidate_bytes"], len(r["visible_reply"].encode()))
            self.assertEqual(model["candidate_fingerprint"], digest(r["visible_reply"]))
            self.assertEqual(model["invocation_count"], 1)
            self.assertEqual(r["provider_invocations"], 1)
            if outcome in ("UNKNOWN", "TIMEOUT"):
                self.assertEqual(model["external_outcome"], "UNKNOWN")
                self.assertIn("REQUIRES_EXPLICIT_DECISION", r["stop_reason"])
            statuses[outcome] = r["status"]
        self.assertEqual(statuses, {"COMPLETE": "complete", "PARTIAL": "partial", "FAILED": "failed", "TIMEOUT": "partial", "UNKNOWN": "partial"})
        self.passed("TS3-08", model_outcomes=list(statuses), canonical_text_statuses=statuses, silent_collapses=0)

    def test_ts3_09_unknown_no_blind_retry_and_crash(self):
        for outcome in ("UNKNOWN", "TIMEOUT"):
            q = request(); q["turn"]["model_script"] = outcome
            directory = self.root / outcome; first = self.call(q, directory)
            for op in (q, operation(q, "observe", request_id="req-1"), operation(q, "resume", request_id="req-1")):
                again = self.child(op, directory)
                self.assertEqual(again["provider_invocations"], 0)
                self.assertEqual(again["model_result"]["invocation_count"], 1)
                self.assertEqual(again["model_result"]["result_fingerprint"], first["model_result"]["result_fingerprint"])
            switch = deepcopy(q); switch["turn"]["model_intent"]["preferred_profile_key"] = "synthetic-large"
            self.rejected(switch, directory, "REQUEST_IDENTITY_CONFLICT")
            self.assertEqual(len(self.call(operation(q, "safe_export"), directory)["events"]), 2)
        for fault in ("AFTER_USER_DURABLE", "AFTER_PROVIDER_INTENT", "AFTER_STUB_FRAME", "BEFORE_DISPLAY"):
            q = request(); directory = self.root / fault
            dead = process(directory, q, fault)
            self.assertEqual((dead.returncode, dead.stdout), (86, ""))
            seen = self.child(operation(q, "observe", request_id="req-1"), directory)
            self.assertEqual(seen["provider_invocations"], 0)
            if fault == "AFTER_USER_DURABLE":
                self.assertEqual(seen["status"], "AWAIT_EXPLICIT_RESUME")
            elif fault != "BEFORE_DISPLAY":
                self.assertEqual(seen["model_result"]["outcome"], "UNKNOWN")
                self.assertEqual(seen["model_result"]["invocation_count"], "UNKNOWN")
                self.assertEqual(seen["model_result"]["invocation_upper_bound"], 1)
            resumed = self.child(operation(q, "resume", request_id="req-1"), directory)
            self.assertEqual(resumed["provider_invocations"], int(fault == "AFTER_USER_DURABLE"))
            again = self.child(operation(q, "resume", request_id="req-1"), directory)
            self.assertEqual(again["provider_invocations"], 0)
            self.assertEqual((again["event_count"], again["terminal_count"]), (2, 1))
            self.assertEqual(again["assistant_event_id"], resumed["assistant_event_id"])
        self.passed("TS3-09", unknown_and_timeout="NO_AUTOMATIC_RETRY", profile_replay="DENY",
                    process_crashes=4, uncertain_invocation_count="UNKNOWN_WITH_UPPER_BOUND_1", duplicate_terminals=0)

    def test_ts3_10_switch_identity_and_authority(self):
        previews = []
        for profile in ("synthetic-small", "synthetic-large", "synthetic-small"):
            previews.append(self.call(request(profile=profile) | {"op": "model_preview"}))
        self.assertEqual(previews[0]["identity"], previews[1]["identity"])
        self.assertEqual(previews[0]["identity"], previews[2]["identity"])
        for p in previews:
            self.assertEqual(p["working_set"]["snapshot"], previews[0]["working_set"]["snapshot"])
            self.assertEqual(p["working_set"]["source_refs"], previews[0]["working_set"]["source_refs"])
        turns = [self.call(request(n, profile=p)) for n, p in enumerate(("synthetic-small", "synthetic-large", "synthetic-small"), 1)]
        for field in ("session_id", "topic_id", "task_id", "universe_id", "access_subject_id", "working_set_id"):
            self.assertEqual(len({r[field] for r in turns}), 1, field)
        self.assertEqual([r["request_id"] for r in turns], ["req-1", "req-2", "req-3"])
        self.assertEqual([r["turn_id"] for r in turns], ["turn-1", "turn-2", "turn-3"])
        self.assertEqual(len({r["attempt_id"] for r in turns}), 3)
        for r in turns:
            self.assertEqual(r["projection"]["working_set"]["snapshot"], turns[0]["projection"]["working_set"]["snapshot"])
        events = self.call(operation(request(), "safe_export"))["events"]
        self.assertEqual([e["sequence_no"] for e in events], list(range(1, 7)))
        for n, r in enumerate(turns):
            self.assertEqual(events[2*n]["event_id"], r["user_event_id"])
            self.assertEqual(events[2*n+1]["receipt"]["fingerprint"], r["receipts"]["assistant"]["fingerprint"])
        self.passed("TS3-10", profile_sequence=["synthetic-small/v1", "synthetic-large/v1", "synthetic-small/v1"],
                    pre_call_identity_drift=0, topic_task_drift=0, authority_permission_drift=0, historical_event_rewrites=0)

    def test_ts3_11_reactivation_capacity_and_compaction(self):
        fixtures = [fixture(text="PUBLIC A"), fixture("beta", text="PUBLIC B "*600), fixture("gamma", text="PUBLIC C")]
        needs = [{"source_id": "alpha"}, {"source_id": "beta"}]
        first = self.call(request(1, "A", "synthetic-large", fixtures=fixtures, needs=needs))
        self.call(request(2, "B", fixtures=fixtures, needs=[{"source_id": "gamma"}]))
        again = self.call(request(3, "A", fixtures=fixtures, needs=[]))
        pack = again["projection"]["context"]
        self.assertEqual(first["working_set_id"], again["working_set_id"])
        self.assertEqual(first["projection"]["working_set"]["source_refs"], again["projection"]["working_set"]["source_refs"])
        self.assertIn("MODEL_PROFILE_CHANGED", pack["rebuild_reasons"])
        self.assertIn("TOPIC_REACTIVATED", pack["rebuild_reasons"])
        self.assertTrue(any(x["ref"].get("source_id") == "beta" for x in pack["omitted"]))
        self.assertNotIn("gamma", encode(again["projection"]))
        for n in range(4, 9):
            again = self.call(request(n, "A", fixtures=fixtures, needs=[]))
        pack = again["projection"]["context"]
        self.assertTrue(pack["compacted"])
        self.assertLessEqual(len(pack["recent_exact_turn_refs"]), 4)
        self.assertEqual(pack["model_budget"]["selected"]["profile_key"], "synthetic-small")
        self.passed("TS3-11", source_links_preserved=True, explicit_profile_rebuild=True,
                    explicit_compaction=True, cross_topic_profile_contamination=0)

    def test_ts3_12_trace_and_secret_boundary(self):
        q = request(); q["turn"]["text"] = "PUBLIC USER BODY TRACE CANARY"
        q["fixtures"][0]["text"] = "PUBLIC SOURCE BODY TRACE CANARY"
        r = self.call(q)
        trace, model = r["model_trace"], r["model_result"]
        for field in ("candidate_profile_refs", "selected_profile", "selection_reason", "capability_requirements",
                      "budget_decision", "fallback_reason", "profile_fingerprint", "context_fingerprint",
                      "call_spec_fingerprint", "attempt_id", "terminal_model_outcome", "retry_decision", "provider_invocation_count"):
            self.assertIn(field, trace)
        self.assertEqual(trace["trace_fingerprint"], digest({k: v for k, v in trace.items() if k != "trace_fingerprint"}))
        wire = encode({"trace": trace, "result": model, "gateway": r["projection"]["model_gateway"]})
        for content in (q["turn"]["text"], q["fixtures"][0]["text"], r["visible_reply"], "journal.sqlite3", "canonical_event_log"):
            self.assertNotIn(content, wire)
        for index, change in enumerate(({"provider_key": "sk-SYNTHETICCANARY123"}, {"api_key": "SYNTHETIC_CANARY"},
                                        {"cookie": "SYNTHETIC_CANARY"}, {"endpoint": "https://provider.invalid"},
                                        {"public_safe": False}, {"synthetic": False})):
            bad = operation(request(), "model_registry"); bad["model_profiles"] = self.profiles()
            bad["model_profiles"][0].update(change)
            path = self.root / f"invalid-profile-{index}"
            error = self.rejected(bad, path)
            self.assertNotIn("CANARY", error); self.assertFalse(path.exists())
        q["fixtures"][0]["public_safe"] = False
        self.rejected(q, self.root / "private", "PUBLIC_SYNTHETIC_FIXTURE_REQUIRED")
        self.passed("TS3-12", trace_fields=13, secret_private_body_leaks=0, unsafe_profile_variants=6, private_fixtures="DENY")

    def test_ts3_13_shell_model_switch_reload(self):
        server = make_server(self.store)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start(); self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        origin = "http://127.0.0.1:" + str(server.server_port)
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/models.js"); response = connection.getresponse()
        self.assertEqual(response.status, 200); script = response.read().decode(); connection.close()
        harness = BROWSER_HARNESS.replace("'owned-home-v1:'", "'owned-home-v3:'").replace(
            "'form','message','submit','resume','next','status','reply'",
            "'form','message','submit','resume','next','status','reply','topic','switch','session','active-topic','model','apply-model','active-model','model-info'").replace(
            "if ('text' in step) elements.message.value = step.text;",
            "if ('text' in step) elements.message.value = step.text; if ('topic' in step) elements.topic.value=step.topic; if ('model' in step) elements.model.value=step.model;")
        def browser(steps, storage=None):
            run = subprocess.run(["node", "-e", harness], input=encode({"origin": origin, "script": script,
                                  "steps": steps, "storage": storage or {}}), text=True, capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(run.stdout)
        body = "PUBLIC USER BODY MUST NEVER PERSIST"
        results = browser([{"event": "submit", "text": body}, {"event": "reload"}, {"event": "next"},
                           {"event": "apply-model", "model": "synthetic-large"}, {"event": "reload"},
                           {"event": "submit", "text": "SECOND PUBLIC TURN"}, {"event": "next"},
                           {"event": "apply-model", "model": "synthetic-small"}, {"event": "reload"},
                           {"event": "submit", "text": "THIRD PUBLIC TURN"}, {"event": "reload"}])
        turns = [c for c in results["calls"] if c["body"]["op"] == "model_turn"]
        self.assertEqual(len(turns), 3)
        self.assertTrue(all(c["data"]["ok"] for c in turns), turns)
        self.assertEqual([c["body"]["turn"]["model_intent"]["preferred_profile_key"] for c in turns],
                         ["synthetic-small", "synthetic-large", "synthetic-small"])
        self.assertEqual(len({c["body"]["turn"]["session_id"] for c in turns}), 1)
        self.assertEqual(len({c["body"]["turn"]["topic_id"] for c in turns}), 1)
        self.assertEqual([c["data"]["result"]["provider_invocations"] for c in turns], [1, 1, 1])
        for call in results["calls"]:
            if call["body"]["op"] == "observe":
                self.assertEqual(call["data"]["result"]["provider_invocations"], 0)
        for key, value in results["writes"]:
            self.assertEqual(key, results["key"])
            fields = json.loads(value)
            self.assertEqual(set(fields), {"session_id", "topic_id", "request_id", "turn_no", "profile_key", "profile_version"})
            self.assertIn(fields["profile_key"], ["synthetic-small", "synthetic-large", "synthetic-capable", "AUTO"])
            self.assertNotIn(body, value); self.assertNotIn("Synthetic model result", value)
        invalid = browser([{"event": "apply-model", "model": "Bearer SYNTHETIC_CANARY"},
                           {"event": "submit", "text": "api_key=SYNTHETIC_CANARY"}])
        self.assertNotIn("SYNTHETIC_CANARY", encode(invalid["writes"]))
        self.assertEqual(invalid["calls"][0]["data"]["error"], "UNSAFE_INPUT")
        # Server selection rejects an incompatible intent even without the UI.
        q = request(); q["turn"]["source_id"] = "local-demo"
        q["turn"]["evidence_needs"] = [{"source_id": "local-demo"}]
        q["turn"]["model_intent"]["required_capabilities"] = ["tool_calls"]
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/v1/turn", encode({k: q[k] for k in ("contract_version", "op", "turn")}),
                           {"Content-Type": "application/json", "X-Owned-Home": "1"})
        response = connection.getresponse(); server_result = json.loads(response.read()); connection.close()
        self.assertEqual(server_result["result"]["provider_invocations"], 0)
        self.assertEqual(server_result["result"]["stop_reason"], "NO_COMPATIBLE_PROFILE")
        self.passed("TS3-13", browser_harness="EXACT_SERVED_JS_NODE_VM_REAL_HTTP", manual_profile_switches=2,
                    stable_session_topic=True, persisted_control_fields=6, raw_content_credential_writes=0, ui_bypass="DENY")

    def test_ts3_14_full_regression(self):
        # TS2's unchanged TS2-14 itself runs every TS1 and A019 case and compares
        # their exact prior fingerprints. No skip/monkeypatch or private oracle.
        run = subprocess.run([sys.executable, "tests/test_owned_home_slice2_conformance.py", "--receipt"],
                             cwd=ROOT, text=True, capture_output=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        receipt = json.loads(run.stdout)
        self.assertEqual((receipt["tests_run"], receipt["failures"], receipt["errors"]), (15, 0, 0))
        self.assertEqual(receipt["conformance_fingerprint"], PRIOR["TS2"])
        self.assertEqual(receipt["matrix"]["TS2-14"], {"status": "PASS", "ts1_tests": 12, "a019_tests": 21,
                                                      "unchanged_behavior_fingerprints": True})
        self.passed("TS3-14", ts2_tests=15, ts1_tests=12, a019_tests=21, unchanged_fingerprints=PRIOR)

    def test_ts3_15_blackbox_network_and_scope(self):
        q = request()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK_FORBIDDEN")), \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS_FORBIDDEN")):
            registry = self.call(operation(q, "model_registry"))
            preview = self.call(q | {"op": "model_preview"})
            result = self.call(q | {"expected_spec": preview["call_spec"]})
            self.assertEqual(result["real_provider_invocations"], 0)
            self.assertEqual(result["model_result"]["spend"], 0)
            for key in ("external_side_effects", "notifications", "continuations", "authority_writes"):
                self.assertEqual(result["counters"][key], 0)
        cli = self.child(operation(q, "observe", request_id="req-1"))
        self.assertEqual(cli["model_result"], result["model_result"])
        self.assertEqual(cli["model_trace"], result["model_trace"])
        for op in ("query_db", "model_registry_object", "provider", "drive", "notify", "semantic_search", "credentials"):
            self.rejected(operation(q, op), expected="OPERATION_NOT_IN_SLICE")
        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(config["project"]["dependencies"], [])
        code = (ROOT / "companion_mind/owned_home/model_gateway.py").read_text()
        for node in ast.walk(ast.parse(code)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                self.assertFalse(any(m.split(".")[0] in {"sqlite3", "socket", "http", "urllib", "requests", "os", "pathlib"} for m in modules))
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
        self.assertEqual(git("hash-object", "companion_mind/owned_home/router.py"), "27628c3a8118eb6ce7ee774eea28e4ce885efb89")
        self.assertEqual(git("hash-object", "tests/test_owned_home_slice2_conformance.py"), "56e90aa6d23c8cd8aaf4cddf5fdd5a90c4a09f48")
        self.passed("TS3-15", network_guard="CONNECT_AND_DNS_DENIED", internal_oracles=0, real_provider_calls=0,
                    credentials=0, live_external_side_effects=0, spend=0, runtime_dependencies=[], cli_equivalence=True)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Slice3Conformance))
    if "--receipt" in sys.argv:
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
        receipt = {"work_order": "WO-A1-A029-P2S3-01", "S0_BASE_SHA": BASE_SHA, "S0_BASE_TREE": BASE_TREE,
                   "head_sha": git("rev-parse", "HEAD"), "head_tree": git("rev-parse", "HEAD^{tree}"),
                   "branch": git("branch", "--show-current"), "working_tree_clean": not bool(git("status", "--porcelain")),
                   "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                   "node": subprocess.check_output(["node", "--version"], text=True).strip(), "runtime_dependencies": [],
                   "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                   "matrix": MATRIX, "conformance_fingerprint": digest(MATRIX),
                   "zero_tolerance_count": 0 if result.wasSuccessful() and len(MATRIX) == 15 else "NOT_EVALUABLE",
                   "status": "READY_FOR_INDEPENDENT_A2_SLICE3_REVIEW / A1 STOP" if result.wasSuccessful() and len(MATRIX) == 15 else "REPAIR_REQUIRED",
                   "limitations": ["Synthetic estimator; no vendor tokenizer parity", "Offline/public-safe fixtures; no provider/credential/live tool",
                                   "Exact served JS Node VM and real loopback HTTP; no native browser rendering claim",
                                   "Process crash; no physical power-cut proof", "No independent A2 verdict"]}
        print(encode(receipt))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
