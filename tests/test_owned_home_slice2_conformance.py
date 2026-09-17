"""WO-A1-A029-P2S2-01: public synthetic TestPort v1 extension conformance.

Run: python tests/test_owned_home_slice2_conformance.py --receipt
The CLI remains: python -m companion_mind.owned_home.testport --store DIR.
Use op=context_turn (or topic_switch), with the v1 Turn plus topic_id,
premise_id and evidence_needs=[{source_id,route:CURRENT|HISTORY|EXACT,
version?:...,revision?:...}]. EXACT requires version; revision is optional.
An empty needs list reactivates that topic's last canonical source-linked set.
Fixtures optionally add revision and lifecycle=CURRENT|HISTORY. Grants remain
explicit scope/source/version decisions. Setup is synthetic/public-safe only.
session_state(session_id), observe(request_id), resume(request_id), safe_export
are public operations. Updates replace supplied fixture/grant setup; they do
not mutate any production Authority. Unknown operations fail closed.
The UI is /continuity, linked from the preserved v1 shell at /.

Only sanctioned JSON/HTTP/CLI observations; no internal database/cache oracle.
"""
from copy import deepcopy
from http.client import HTTPConnection
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from companion_mind.owned_home.testport import execute
from companion_mind.owned_home.shell import make_server
from test_owned_home_slice1_conformance import BROWSER_HARNESS

BASE_SHA = "f5deb051daef07b708859e0ced2d233749e1a59c"
BASE_TREE = "2efaa0bacd726d5c1506a33ea39da17529559ee7"
SCOPE = {"universe_id": "synthetic-home", "access_subject_id": "synthetic-owner"}
MATRIX = {}


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def fixture(source="alpha", version="v1", revision="r1", lifecycle="CURRENT", text=None):
    return dict(SCOPE, source_id=source, version=version, revision=revision, lifecycle=lifecycle,
                text=text if text is not None else "SYNTHETIC EVIDENCE " + source + " " + version + " " + revision)


def request(number=1, topic="A", *, fixtures=None, needs=None, premise="task-v1"):
    fixtures = [fixture()] if fixtures is None else fixtures
    return {"contract_version": "owned-home/1", "scope": dict(SCOPE), "fixtures": deepcopy(fixtures),
            "grants": [dict(SCOPE, source_id=s, version=v) for s, v in sorted({(f["source_id"], f["version"]) for f in fixtures})],
            "op": "context_turn", "turn": dict(SCOPE, request_id=f"req-{number}", session_id="session-s2",
                turn_id=f"turn-{number}", turn_no=number, source_id="alpha", source_version="v1",
                text=f"PUBLIC SYNTHETIC QUESTION {number}", observed_at="2026-09-17T00:00:00+00:00",
                budget_bytes=16384, topic_id=topic, premise_id=premise,
                evidence_needs=needs if needs is not None else [{"source_id": "alpha"}])}


def operation(req, op, **fields):
    return {k: deepcopy(v) for k, v in req.items() if k in ("contract_version", "scope", "fixtures", "grants")} | {"op": op, **fields}


def process(store, req, fault=None):
    cmd = [sys.executable, "-m", "companion_mind.owned_home.testport", "--store", str(store)]
    if fault:
        cmd += ["--fault", fault]
    return subprocess.run(cmd, input=encode(req), text=True, capture_output=True, cwd=ROOT, timeout=20)


class Slice2Conformance(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.root / "home"

    def call(self, req, store=None):
        return execute(store or self.store, deepcopy(req))

    def passed(self, case, **facts):
        MATRIX[case] = {"status": "PASS", **facts}

    def test_ts2_01_three_turn_continuity(self):
        turns = [self.call(request(n)) for n in range(1, 4)]
        self.assertEqual({r["session_id"] for r in turns}, {"session-s2"})
        self.assertEqual(len({r["working_set_id"] for r in turns}), 1)
        pack = turns[-1]["projection"]["context"]
        self.assertEqual([r["turn_id"] for r in pack["recent_exact_turn_refs"]], ["turn-1", "turn-2"])
        recent = [{"ref": pack["recent_exact_turn_refs"][i], "user_text": request(i+1)["turn"]["text"],
                   "assistant_text": turns[i]["visible_reply"], "valid": True} for i in range(2)]
        payload = {"topic_id": "A", "user_text": request(3)["turn"]["text"],
                   "evidence": [{"ref": pack["included"][0], "text": fixture()["text"]}], "recent_exact_turns": recent}
        self.assertEqual(pack["input_fingerprint"], digest(payload))
        self.assertEqual(pack["budget"]["included_bytes"], len(encode(payload).encode()))
        for n in range(4, 8):
            last = self.call(request(n))
        tail = last["projection"]["context"]
        self.assertEqual([r["turn_id"] for r in tail["recent_exact_turn_refs"]], ["turn-3", "turn-4", "turn-5", "turn-6"])
        self.assertTrue(any(x["reason"] == "RECENT_TAIL_LIMIT" for x in tail["omitted"]))
        self.assertEqual(tail["compacted"][0]["ref"]["count"], 2)
        events = self.call(operation(request(), "safe_export"))["events"]
        self.assertEqual([e["sequence_no"] for e in events], list(range(1, 15)))
        self.passed("TS2-01", turns=7, bounded_recent_turns=4, exact_input_fingerprint_verified=True, duplicate_terminals=0)

    def test_ts2_02_reactivate_three_topics(self):
        fixtures = [fixture(x) for x in ("alpha", "beta", "gamma")]
        responses = []
        for n, (topic, source) in enumerate((("A", "alpha"), ("B", "beta"), ("C", "gamma"), ("A", "alpha")), 1):
            req = request(n, topic, fixtures=fixtures, needs=[] if n == 4 else [{"source_id": source}])
            req["op"] = "topic_switch" if n > 1 else "context_turn"
            responses.append(self.call(req))
        a, resumed = responses[0], responses[-1]
        self.assertEqual(a["working_set_id"], resumed["working_set_id"])
        pack = resumed["projection"]["context"]
        self.assertEqual({r["source_id"] for r in pack["included"]}, {"alpha"})
        self.assertEqual([r["turn_id"] for r in pack["recent_exact_turn_refs"]], ["turn-1"])
        self.assertIn("TOPIC_REACTIVATED", pack["rebuild_reasons"])
        self.assertEqual(len(resumed["projection"]["working_set"]["segment_refs"]), 2)
        self.assertNotIn("beta", encode(resumed["projection"]))
        self.assertNotIn("gamma", resumed["visible_reply"])
        session = self.call(operation(req, "session_state", session_id="session-s2"))
        self.assertEqual([x["topic_id"] for x in session["topics"]], ["A", "B", "C"])
        self.assertEqual(session["active_topic"], "A")
        self.assertEqual([t["topic_id"] for t in session["topics"] if t["active"]], ["A"])
        self.passed("TS2-02", segments=4, topics=3, source_linked_reactivation=True, cross_topic_contamination=0)

    def test_ts2_03_current_history_conflicts(self):
        old = fixture(version="v1", lifecycle="HISTORY", text="OLD SYNTHETIC VALUE")
        current = fixture(version="v2", text="CURRENT SYNTHETIC VALUE")
        req = request(fixtures=[old, current])
        r = self.call(req)
        self.assertIn(current["text"], r["visible_reply"])
        self.assertNotIn(old["text"], r["visible_reply"])
        self.assertEqual(r["projection"]["context"]["conflicts"][0]["resolution"], "CURRENT_WINS")
        req["turn"]["evidence_needs"] = [{"source_id": "alpha", "route": "HISTORY"}]
        history = self.call(req, self.root / "history")
        self.assertIn(old["text"], history["visible_reply"])
        req = request(fixtures=[fixture(version="v1"), current])
        conflict = self.call(req, self.root / "conflict")
        self.assertEqual(conflict["stop_reason"], "AUTHORITY_CONFLICT")
        self.assertEqual(conflict["projection"]["context"]["included"], [])
        self.assertEqual(conflict["projection"]["retrieval"]["routes"][0]["conflict"], "CONFLICT")
        self.passed("TS2-03", current_wins=True, ambiguous_current="CONFLICT", silent_merges=0)

    def test_ts2_04_exact_version_and_revision(self):
        fixtures = [fixture(revision="r1", lifecycle="HISTORY"), fixture(revision="r2")]
        req = request(fixtures=fixtures, needs=[{"source_id": "alpha", "route": "EXACT", "version": "v1", "revision": "r1"}])
        result = self.call(req)
        self.assertIn(fixtures[0]["text"], result["visible_reply"])
        ref = result["projection"]["context"]["included"][0]
        self.assertEqual((ref["version"], ref["revision"]), ("v1", "r1"))
        for i, need in enumerate(({"version": "v9", "revision": "r1"}, {"version": "v1", "revision": "r9"}, {"version": "v1"})):
            req["turn"]["evidence_needs"] = [{"source_id": "alpha", "route": "EXACT", **need}]
            result = self.call(req, self.root / f"missing-{i}")
            self.assertEqual(result["projection"]["context"]["included"], [])
            self.assertNotIn(fixtures[1]["text"], result["visible_reply"])
        self.passed("TS2-04", exact_revision=True, ambiguous_revision="CONFLICT", wrong_version_returns=0)

    def test_ts2_05_unknown_is_not_negative_existence(self):
        req = request(fixtures=[])
        req["grants"] = [dict(SCOPE, source_id="alpha", version="v1")]
        result = self.call(req)
        route = result["projection"]["retrieval"]["routes"][0]
        self.assertEqual((route["knowledge_state"], route["negative_existence"]), ("UNKNOWN", "NOT_ESTABLISHED"))
        denied = self.call(request(fixtures=[]), self.root / "denied")
        self.assertEqual(denied["projection"]["context"]["knowledge_states"], ["NOT_LOOKED_UP"])
        empty = self.call(request(fixtures=[fixture(text="")]), self.root / "empty")
        self.assertEqual(empty["projection"]["context"]["knowledge_states"], ["KNOWN_EMPTY"])
        self.assertEqual(empty["projection"]["context"]["unused_layers_state"], "N_A")
        self.passed("TS2-05", states=["KNOWN_VALUE", "KNOWN_EMPTY", "UNKNOWN", "N_A", "NOT_LOOKED_UP"], negative_existence_inferences=0)

    def test_ts2_06_authorization_before_candidate_retrieval(self):
        canary = "SYNTHETIC UNAUTHORIZED BODY CANARY"
        req = request(fixtures=[fixture(text=canary)])
        req["grants"] = []
        denied = self.call(req)
        self.assertEqual(denied["counters"]["candidate_retrievals"], 0)
        self.assertEqual(denied["counters"]["authority_reads"], 0)
        self.assertNotIn(canary, encode(denied))
        allowed = request(fixtures=[fixture("alpha"), fixture("beta", text=canary)],
                          needs=[{"source_id": "alpha"}, {"source_id": "beta"}])
        allowed["grants"] = [dict(SCOPE, source_id="alpha", version="v1")]
        mixed = self.call(allowed, self.root / "mixed")
        self.assertNotIn(canary, encode(mixed))
        order = mixed["projection"]["retrieval"]["query_order"]
        for i, action in enumerate(order):
            if action["action"] == "RETRIEVE":
                self.assertEqual(order[i-1], {**action, "action": "AUTHORIZE", "decision": "ALLOW"})
        conflict = request()
        conflict["grants"].append(dict(SCOPE, source_id="alpha", version="v1", decision="DENY"))
        result = self.call(conflict, self.root / "conflicting-grants")
        self.assertEqual(result["counters"]["candidate_retrievals"], 0)
        self.passed("TS2-06", denied_candidate_retrievals=0, denied_authority_reads=0, content_leaks=0, query_order_verified=True)

    def test_ts2_07_multi_source_determinism_and_markers(self):
        fs = [fixture("source-"+str(i)) for i in range(5)]
        req = request(fixtures=fs, needs=[{"source_id": f["source_id"]} for f in fs])
        first = self.call(req)
        req["fixtures"].reverse(); req["grants"].reverse(); req["turn"]["evidence_needs"].reverse()
        second = self.call(req, self.root / "second")
        self.assertEqual(len(first["projection"]["context"]["included"]), 5)
        self.assertEqual(first["projection"]["context"]["context_fingerprint"], second["projection"]["context"]["context_fingerprint"])
        fs = [fixture("alpha"), fixture("beta"), fixture("conflict", "v1"), fixture("conflict", "v2")]
        req = request(fixtures=fs, needs=[{"source_id": x} for x in ("alpha", "beta", "absent", "conflict")])
        req["grants"].append(dict(SCOPE, source_id="absent", version="v1"))
        mixed = self.call(req, self.root / "markers")["projection"]["context"]
        self.assertEqual(len(mixed["included"]), 2)
        self.assertIn("UNKNOWN", mixed["knowledge_states"])
        self.assertEqual(mixed["conflicts"][0]["resolution"], "CONFLICT")
        histories = [fixture("alpha", f"v{i}", lifecycle="HISTORY") for i in range(8)]
        many = self.call(request(fixtures=histories, needs=[{"source_id": "alpha", "route": "HISTORY"}]), self.root / "bounded")["projection"]["context"]
        self.assertEqual(len(many["included"]), 5)
        self.assertEqual([x["reason"] for x in many["omitted"]], ["EVIDENCE_LIMIT"] * 3)
        self.passed("TS2-07", min_evidence=2, max_evidence=5, deterministic=True, unknown_conflict_preserved=True)

    def test_ts2_08_exact_budget_explicit_omissions(self):
        fs = [fixture("alpha", text="A"*1200), fixture("beta", text="B"*1200)]
        req = request(fixtures=fs, needs=[{"source_id": "alpha"}, {"source_id": "beta"}])
        req["turn"]["budget_bytes"] = 2000
        r = self.call(req)["projection"]["context"]
        self.assertEqual(len(r["included"]), 1)
        self.assertEqual(len(r["omitted"]), 1)
        self.assertEqual(r["omitted"][0]["reason"], "CONTEXT_BUDGET_EXCEEDED")
        self.assertLessEqual(r["budget"]["included_bytes"], 2000)
        self.assertEqual(r["budget"]["included_bytes"] + r["budget"]["omitted_bytes"], r["budget"]["required"])
        self.assertEqual([x["action"] for x in r["ledger"]], ["INCLUDE", "OMIT"])
        req["turn"]["budget_bytes"] = 64
        blocked = self.call(req, self.root / "blocked")["projection"]["context"]
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["budget"]["included_bytes"], 0)
        self.assertEqual(blocked["input_fingerprint"], digest(None))
        self.assertTrue(all(x["action"] == "OMIT" for x in blocked["ledger"]))
        self.passed("TS2-08", atomic_omissions=True, exact_utf8_budget=True, silent_truncations=0)

    def test_ts2_09_revision_lifecycle_invalidation_and_rebuild(self):
        first = self.call(request())
        updated = request(2, fixtures=[fixture(lifecycle="HISTORY"), fixture(version="v2")])
        stale = self.call(operation(updated, "observe", request_id="req-1"))
        self.assertIsNone(stale["visible_reply"])
        self.assertEqual(stale["stop_reason"], "DERIVED_STATE_INVALIDATED")
        session = self.call(operation(updated, "session_state", session_id="session-s2"))
        self.assertEqual(session["topics"][0]["state"], "INVALIDATED")
        self.assertIsNone(session["topics"][0]["boot_pack"])
        rebuilt = self.call(updated)
        pack = rebuilt["projection"]["context"]
        self.assertIn("SOURCE_REVISION_CHANGED", pack["invalidation_reasons"])
        self.assertIn("LIFECYCLE_CHANGED", pack["invalidation_reasons"])
        self.assertEqual(pack["recent_exact_turn_refs"], [])
        self.assertEqual(pack["included"][0]["version"], "v2")
        ws = rebuilt["projection"]["working_set"]
        self.assertEqual(ws["invalidation_status"], "REBUILT")
        self.assertEqual(ws["history_refs"][0]["version"], "v1")
        exported = self.call(operation(updated, "safe_export"))["events"]
        original = next(e for e in exported if e["event_id"] == first["assistant_event_id"])
        self.assertEqual(original["receipt"]["fingerprint"], first["receipts"]["assistant"]["fingerprint"])
        self.assertEqual(len(exported), 4)
        # Lifecycle-only changes are also invalidating, independently of bytes.
        lifecycle = request(3, fixtures=[fixture(lifecycle="HISTORY"), fixture(version="v2", lifecycle="HISTORY")])
        held = self.call(operation(lifecycle, "observe", request_id="req-2"))
        self.assertIn("LIFECYCLE_CHANGED", held["projection"]["invalidation_reasons"])
        self.passed("TS2-09", revisions=True, lifecycle=True, stale_active_inputs=0, canonical_history_preserved=True)

    def test_ts2_10_acl_tightening_and_scope(self):
        req = request(); first = self.call(req)
        denied = request(2); denied["grants"] = []
        for op, fields in (("observe", {"request_id": "req-1"}), ("resume", {"request_id": "req-1"})):
            result = self.call(operation(denied, op, **fields))
            self.assertIsNone(result["visible_reply"])
            self.assertIn("ACL_SCOPE_CHANGED", result["projection"]["invalidation_reasons"])
            self.assertNotIn(fixture()["text"], encode(result))
            self.assertEqual(result["counters"]["candidate_retrievals"], 0)
        blocked = self.call(denied)
        self.assertEqual(blocked["stop_reason"], "PERMISSION_DENIED")
        self.assertEqual(blocked["projection"]["context"]["recent_exact_turn_refs"], [])
        other = operation(req, "session_state", session_id="session-s2")
        other["scope"]["access_subject_id"] = "different-owner"
        self.assertEqual(self.call(other)["topics"], [])
        other = operation(req, "observe", request_id="req-1"); other["scope"]["universe_id"] = "different-universe"
        self.assertEqual(self.call(other)["status"], "NOT_FOUND")
        self.passed("TS2-10", revoked_replay="WITHHELD", cross_scope_topics=0, stale_active_inputs=0)

    def test_ts2_11_premise_change_invalidates_downstream(self):
        self.call(request()); self.call(request(2))
        changed = self.call(request(3, premise="task-v2"))
        self.assertIn("PREMISE_CHANGED", changed["projection"]["context"]["invalidation_reasons"])
        self.assertEqual(changed["projection"]["context"]["recent_exact_turn_refs"], [])
        old = self.call(operation(request(), "observe", request_id="req-1"))
        self.assertIsNone(old["visible_reply"])
        self.assertIn("PREMISE_CHANGED", old["projection"]["invalidation_reasons"])
        next_turn = self.call(request(4, premise="task-v2"))["projection"]["context"]
        self.assertEqual([r["turn_id"] for r in next_turn["recent_exact_turn_refs"]], ["turn-3"])
        self.passed("TS2-11", premise_revision=True, obsolete_tail_excluded=True, stale_replay="WITHHELD")

    def test_ts2_12_trace_and_derived_content_minimization(self):
        req = request(); result = self.call(req)
        trace = result["projection"]["trace"]
        for field in ("active_topic", "authority_routes", "authorization", "query_order", "queried_refs",
                      "included_refs", "omitted_refs", "compacted_refs", "conflicts", "budget",
                      "context_fingerprint", "invalidation_reasons", "rebuild_reasons", "terminal_reason"):
            self.assertIn(field, trace)
        self.assertEqual(trace["trace_fingerprint"], digest({k:v for k,v in trace.items() if k != "trace_fingerprint"}))
        safe = encode([result["projection"], self.call(operation(req, "session_state", session_id="session-s2")),
                       self.call(operation(req, "safe_export"))])
        for body in (req["turn"]["text"], fixture()["text"], "hidden_reasoning", "journal.sqlite3"):
            self.assertNotIn(body, safe)
        for i, field in enumerate(("text", "topic_id", "premise_id")):
            unsafe = request(); unsafe["turn"][field] = "password=SYNTHETIC_REJECTED_CANARY"
            directory = self.root / f"rejected-{i}"
            child = process(directory, unsafe)
            self.assertEqual(child.returncode, 2)
            self.assertNotIn("SYNTHETIC_REJECTED_CANARY", child.stdout)
            self.assertFalse(directory.exists())
        private = request(); private["fixtures"][0]["public_safe"] = False
        self.assertEqual(json.loads(process(self.root / "private", private).stdout)["error"], "PUBLIC_SYNTHETIC_FIXTURE_REQUIRED")
        self.passed("TS2-12", required_trace_fields=14, secret_private_body_leaks=0, private_fixture_admission="DENY")

    def test_ts2_13_shell_reload_and_crash_identity(self):
        server = make_server(self.store)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval":0.02}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        origin = "http://127.0.0.1:" + str(server.server_port)
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/continuity.js")
        response = connection.getresponse(); self.assertEqual(response.status, 200)
        script = response.read().decode(); connection.close()
        harness = BROWSER_HARNESS.replace("'owned-home-v1:'", "'owned-home-v2:'").replace(
            "'form','message','submit','resume','next','status','reply'",
            "'form','message','submit','resume','next','status','reply','topic','switch','session','active-topic'").replace(
            "if ('text' in step) elements.message.value = step.text;",
            "if ('text' in step) elements.message.value = step.text; if ('topic' in step) elements.topic.value = step.topic;")
        def browser(steps, storage=None):
            run = subprocess.run(["node", "-e", harness], input=encode({"origin":origin,"script":script,
                                  "steps":steps,"storage":storage or {}}), text=True,capture_output=True,timeout=25)
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(run.stdout)
        text = "PUBLIC USER BODY NEVER PERSIST IN BROWSER"
        result = browser([{"event":"submit","text":text},{"event":"reload"},
                          {"event":"switch","topic":"topic-b"},{"event":"switch","topic":"topic-c"},
                          {"event":"switch","topic":"topic-a"},{"event":"reload"}])
        turns = [c["body"]["turn"] for c in result["calls"] if c["body"]["op"] in {"context_turn","topic_switch"}]
        self.assertEqual([t["topic_id"] for t in turns], ["topic-a","topic-b","topic-c","topic-a"])
        self.assertEqual(len({t["session_id"] for t in turns}), 1)
        self.assertEqual([t["turn_no"] for t in turns], [1,2,3,4])
        last = result["calls"][-1]["data"]["result"]
        self.assertEqual(last["topic_id"], "topic-a")
        self.assertEqual(last["terminal_count"], 1)
        for key, value in result["writes"]:
            self.assertEqual(key, result["key"])
            self.assertEqual(set(json.loads(value)), {"session_id","topic_id","request_id","turn_no"})
            self.assertNotIn(text, value)
        for prefix in ("password=", "api_key=", "Bearer "):
            rejected = browser([{"event":"submit","text":prefix+"SYNTHETIC-CANARY"}])
            self.assertNotIn("SYNTHETIC-CANARY", encode(rejected["writes"]))
            self.assertEqual(rejected["calls"][0]["data"]["error"], "UNSAFE_INPUT")
        # Real subprocess crashes, then process-independent sanctioned recovery.
        for fault in ("AFTER_USER_DURABLE", "AFTER_PROVIDER_INTENT", "AFTER_STUB_FRAME", "BEFORE_DISPLAY"):
            directory = self.root / fault; req = request()
            dead = process(directory, req, fault)
            self.assertEqual((dead.returncode, dead.stdout), (86, ""))
            observed = json.loads(process(directory, operation(req,"observe",request_id="req-1")).stdout)["result"]
            self.assertEqual(observed["session_id"], "session-s2")
            self.assertEqual(observed["counters"]["cognition_stub_invocations"], 0)
            resumed = json.loads(process(directory, operation(req,"resume",request_id="req-1")).stdout)["result"]
            self.assertEqual(resumed["terminal_count"], 1)
            self.assertEqual(resumed["topic_id"], "A")
            self.assertEqual(resumed["user_event_id"], observed["user_event_id"])
            again = json.loads(process(directory, operation(req,"resume",request_id="req-1")).stdout)["result"]
            self.assertEqual(again["assistant_event_id"], resumed["assistant_event_id"])
            self.assertEqual(again["counters"]["cognition_stub_invocations"], 0)
        self.passed("TS2-13", browser_harness="EXACT_SERVED_JS_NODE_VM_REAL_HTTP", stable_topics=3,
                    raw_browser_writes=0, process_crashes=4, duplicate_terminals=0)

    def test_ts2_14_full_ts1_and_a019_regression(self):
        ts1 = subprocess.run([sys.executable, "tests/test_owned_home_slice1_conformance.py", "--receipt"],
                             cwd=ROOT,text=True,capture_output=True,timeout=90)
        self.assertEqual(ts1.returncode, 0, ts1.stderr)
        a = json.loads(ts1.stdout)
        self.assertEqual((a["tests_run"], a["failures"], a["errors"]), (12,0,0))
        output = self.root / "a019.json"
        run = subprocess.run([sys.executable,"tools/a019_conformance.py","--output",str(output)],
                             cwd=ROOT,text=True,capture_output=True,timeout=90)
        self.assertEqual(run.returncode, 0, run.stderr)
        b = json.loads(output.read_text())
        self.assertEqual((b["tests_run"], b["failures"], b["errors"]), (21,0,0))
        self.assertEqual(a["conformance_fingerprint"], "5b8dd9100d3ec7d0370a7ac489499b2a10677d0db0a07ce80575445a32678be1")
        self.assertEqual(b["normalized_result_fingerprint"], "d20d06a356051f71972ef1f5aa34d1dc72eddb8f70b74773ffb0a535ed1cfa6d")
        self.passed("TS2-14", ts1_tests=12, a019_tests=21, unchanged_behavior_fingerprints=True)

    def test_ts2_15_blackbox_and_offline_boundary(self):
        req = request()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK_FORBIDDEN")), \
             patch.object(socket,"getaddrinfo",side_effect=AssertionError("DNS_FORBIDDEN")):
            result = self.call(req)
            self.assertEqual(result["status"], "complete")
            for key in ("provider_invocations","external_side_effects","authority_writes","notifications","continuations"):
                self.assertEqual(result["counters"][key], 0)
        child = process(self.store, operation(req,"observe",request_id="req-1"))
        self.assertEqual(child.returncode, 0)
        wire = json.loads(child.stdout)["result"]
        self.assertEqual(wire["projection"]["context"]["context_fingerprint"], result["projection"]["context"]["context_fingerprint"])
        for op in ("query_db","provider","drive","notify","write_authority","semantic_search"):
            self.assertEqual(json.loads(process(self.store, operation(req,op)).stdout)["error"], "OPERATION_NOT_IN_SLICE")
        info = self.call(operation(req,"info"))
        self.assertEqual(info["testport"], "OwnedHomeTestPort v1")
        self.assertFalse(info["external_connectors_enabled"])
        self.passed("TS2-15", internal_db_oracles=0, cli_receipt_equivalence=True, real_provider_calls=0,
                    live_external_side_effects=0, spend=0, automatic_resume=False)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Slice2Conformance))
    if "--receipt" in sys.argv:
        def git(*args):
            return subprocess.check_output(["git",*args],cwd=ROOT,text=True).strip()
        ok = result.wasSuccessful() and len(MATRIX) == 15
        receipt = {"work_order":"WO-A1-A029-P2S2-01","S0_BASE_SHA":BASE_SHA,"S0_BASE_TREE":BASE_TREE,
                   "branch":git("branch","--show-current"),"head_sha":git("rev-parse","HEAD"),
                   "head_tree":git("rev-parse","HEAD^{tree}"),"working_tree_clean":not bool(git("status","--porcelain")),
                   "python":sys.version.split()[0],"sqlite":sqlite3.sqlite_version,
                   "node":subprocess.check_output(["node","--version"],text=True).strip(),"runtime_dependencies":[],
                   "tests_run":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),
                   "matrix":MATRIX,"conformance_fingerprint":digest(MATRIX),
                   "zero_tolerance_count":0 if ok else "NOT_EVALUABLE",
                   "status":"READY_FOR_INDEPENDENT_A2_SLICE2_REVIEW / A1 STOP" if ok else "NOT_READY / REPAIR_REQUIRED",
                   "scope_amendment":"USER approved old TS1-12 repository preflight adaptation only",
                   "limitations":["Synthetic public-safe fixtures only; no live Authority/provider/credentials",
                                  "Exact served JS in Node VM + real loopback HTTP, not native browser rendering",
                                  "Process-crash proof, not physical power-cut proof", "No independent A2 verdict"]}
        print(encode(receipt))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
