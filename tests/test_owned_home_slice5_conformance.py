"""WO-A1-A029-P2S5-01 v0.1: public CLI/TestPort/loopback conformance.

Run: python tests/test_owned_home_slice5_conformance.py --receipt
Fixture helpers below are complete black-box examples. human_owners/human_now
are trusted synthetic setup, never browser input. HumanResponse text is a bounded
public command, not arbitrary human prose or a P3/P4 authorization. Call
human_request, human_respond, then explicit human_resume. human_observe never
executes a hop. Recovery UNKNOWN consumes the single reservation and cannot retry.
No assertions inspect internal control files or Journal tables. Source/Git reads
are limited to the repository/dependency gate. Raw command evidence stays in A019;
safe_export exposes evidence identities, fingerprints and public ingest receipts.
"""
from copy import deepcopy
from http.client import HTTPConnection
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
from companion_mind.owned_home.testport import execute, OwnedHomeTestPort
from companion_mind.owned_home.contracts import Scope
from companion_mind.owned_home.human_control import OwnerFixture
from companion_mind.owned_home.shell import make_server
from test_owned_home_slice1_conformance import BROWSER_HARNESS
from test_owned_home_slice2_conformance import SCOPE, encode, digest
from test_owned_home_slice4_conformance import request as tool_request, operation as tool_operation

BASE_SHA = "40389094886cd1f8570919459f38dc22083c0e37"
BASE_TREE = "3a0b5b5120c15504db10f41e11a0dc5997a779ee"
PRIOR = {"TS4": "25c35f920ac9e4921dcc10dd470a4c77e1c8889ecde13e84d20705e0446ec26c",
         "TS3": "a0ec993b577fca1565c9104ac03a3808857b82bdec234f688c11d4530629c58f",
         "TS2": "05ac94539f2b7be4829e3a3405b9f053f21f1c2ba2e3fcff4fc81bb4293f5999",
         "TS1": "5b8dd9100d3ec7d0370a7ac489499b2a10677d0db0a07ce80575445a32678be1",
         "A019": "d20d06a356051f71972ef1f5aa34d1dc72eddb8f70b74773ffb0a535ed1cfa6d"}
IDS = ("human_request_id", "request_id", "trace_id", "goal_id", "task_id", "session_id", "turn_id", "turn_no",
       "universe_id", "access_subject_id", "owner_id")
MATRIX = {}


def request(number=1, **changes):
    req = dict(SCOPE, human_request_id=f"human-{number}", request_id=f"request-{number}", trace_id=f"trace-{number}",
               goal_id=f"goal-{number}", task_id=f"task-{number}", session_id="session-s5", turn_id=f"turn-{number}",
               turn_no=number, owner_id="speaker-1")
    req.update(changes)
    return {"contract_version": "owned-home/1", "scope": dict(SCOPE), "op": "human_request", "human_request": req,
            "human_owners": [dict(SCOPE, owner_id="speaker-1")]}


def operation(req, op, **fields):
    return {k: deepcopy(v) for k, v in req.items() if k in ("contract_version", "scope", "human_owners", "human_now")} | {"op": op, **fields}


def answer(req, result, text=" CONTINUE ", **changes):
    response = {k: result["human_request"][k] for k in IDS}
    response.update(request_fingerprint=result["human_request"]["request_fingerprint"], response_id="response-1", text=text)
    response.update(changes)
    return operation(req, "human_respond", human_response=response)


def resume(req, result, op="human_resume"):
    return operation(req, op, human_request_id=result["human_request"]["human_request_id"],
                     request_fingerprint=result["human_request"]["request_fingerprint"])


def observe(req):
    return operation(req, "human_observe", human_request_id=req["human_request"]["human_request_id"])


def process(store, req, fault=None):
    command = [sys.executable, "-m", "companion_mind.owned_home.testport", "--store", str(store)]
    if fault:
        command += ["--fault", fault]
    return subprocess.run(command, input=encode(req), text=True, capture_output=True, cwd=ROOT, timeout=25)


class Slice5Conformance(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root, self.store = Path(temp.name), Path(temp.name) / "home"

    def call(self, req, store=None):
        return execute(store or self.store, deepcopy(req))

    def child(self, req, store=None):
        result = process(store or self.store, req)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)["result"]

    def rejected(self, req, expected=None, store=None):
        result = process(store or self.store, req)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        error = json.loads(result.stdout)["error"]
        if expected:
            self.assertEqual(error, expected)
        return error

    def passed(self, case, **facts):
        MATRIX[case] = {"status": "PASS", **facts}

    def test_ts5_01_contract_determinism(self):
        q = request(); a = self.call(q | {"op": "human_preview"})
        b = self.call(dict(reversed(list(q.items()))) | {"op": "human_preview"}, self.root / "other")
        self.assertEqual(a, b)
        req = a["human_request"]
        self.assertEqual(req["request_fingerprint"], digest({k: v for k, v in req.items() if k != "request_fingerprint"}))
        for field in IDS:
            changed = request(**{field: 2 if field == "turn_no" else "different"})
            if field in ("universe_id", "access_subject_id"):
                self.rejected(changed, "SCOPE_DENIED")
            else:
                c = self.call(changed | {"op": "human_preview"})
                if field == "owner_id":
                    self.assertEqual(c["status"], "HOLD")
                else:
                    self.assertNotEqual(c["human_request"]["request_fingerprint"], req["request_fingerprint"])
        self.rejected(request(contract_version="human-request/2"), "HUMAN_REQUEST_VERSION_MISMATCH")
        self.call(q)
        self.rejected(request(trace_id="new-trace"), "HUMAN_REQUEST_CONFLICT")
        self.rejected(request(2, goal_id="goal-1", task_id="task-1"), "HUMAN_TASK_IDENTITY_CONFLICT")
        self.passed("TS5-01", contract_version=req["contract_version"], request_fingerprint=req["request_fingerprint"],
                    deterministic=True, identity_drift_fields=len(IDS), same_task_budget_reset="DENY", sample_request=q)

    def test_ts5_02_exact_binding(self):
        q = request(); a = self.call(q); errors = {}
        for key in (*IDS, "request_fingerprint"):
            value = 2 if key == "turn_no" else "0" * 64 if key == "request_fingerprint" else "other"
            errors[key] = self.rejected(answer(q, a, **{key: value}))
        self.assertEqual(self.call(observe(q))["continuation_count"], 0)
        for field in ("universe_id", "access_subject_id"):
            foreign = observe(q); foreign["scope"][field] = "other"
            self.rejected(foreign, "SCOPE_DENIED")
        self.passed("TS5-02", invalid_bindings=errors, resume_count=0, cross_scope="DENY",
                    exact_identity={k: a["human_request"][k] for k in IDS})

    def test_ts5_03_stale_expired_cancelled(self):
        q = request(); a = self.call(q)
        self.rejected(answer(q, a, request_fingerprint="f" * 64), "INVALID_RESPONSE_BINDING")
        expired = operation(q, "human_observe", human_request_id="human-1") | {"human_now": "2026-09-19T00:00:00+00:00"}
        r = self.call(expired)
        self.assertEqual((r["status"], r["stop_reason"], r["continuation_count"]), ("STOP", "EXPIRED", 0))
        self.rejected(answer(q, a), "INVALID_RESPONSE_LIFECYCLE")
        q2 = request(2); a2 = self.call(q2); cancel = self.call(resume(q2, a2, "human_cancel"))
        self.assertEqual(cancel["stop_reason"], "CANCELLED")
        self.rejected(answer(q2, a2), "INVALID_RESPONSE_LIFECYCLE")
        self.assertEqual(self.call(resume(q2, a2))["continuation_count"], 0)
        self.assertEqual(self.call(resume(q, a))["continuation_count"], 0)
        self.passed("TS5-03", stale="INVALID_RESPONSE_BINDING", expired="INVALID_RESPONSE_LIFECYCLE",
                    cancelled="INVALID_RESPONSE_LIFECYCLE", clock_rollback_resurrection=0, resume_count=0)

    def test_ts5_04_replay_conflict(self):
        q = request(); a = self.call(q); response = answer(q, a)
        b = self.call(response); c = self.child(response)
        self.assertFalse(b["response_replay"]); self.assertTrue(c["response_replay"])
        self.assertEqual(b["human_response"], c["human_response"])
        for change in ({"text": "HOLD"}, {"text": "CONTINUE"}, {"response_id": "different"}):
            self.rejected(answer(q, a, **change), "HUMAN_RESPONSE_CONFLICT")
        self.assertEqual(self.call(observe(q))["continuation_count"], 0)
        d = self.call(resume(q, a)); self.assertTrue(self.child(response)["response_replay"])
        self.assertEqual(self.call(resume(q, a))["continuation_count"], 1)
        self.passed("TS5-04", response_version=b["human_response"]["contract_version"],
                    response_fingerprint=b["human_response"]["response_fingerprint"],
                    changed_duplicate="HUMAN_RESPONSE_CONFLICT", identical_replay_increment=0,
                    raw_fingerprint=b["human_response"]["raw_payload_fingerprint"], terminal_count=d["terminal_count"])

    def test_ts5_05_request_durable_before_wait(self):
        q = request(); a = self.child(q); b = self.child(observe(q))
        self.assertEqual(a["human_request"], b["human_request"])
        self.assertEqual(a["state"], "WAITING")
        self.assertEqual([e["kind"] for e in a["evidence"]], ["REQUEST"])
        self.assertLess(a["presentation_order"].index("A019_REQUEST_DURABLE"), a["presentation_order"].index("DISPLAY"))
        self.assertEqual(self.child(q)["human_requests_created"], 0)
        exported = self.call(operation(q, "safe_export"))["events"]
        self.assertEqual(len(exported), 1)
        self.passed("TS5-05", reload_identity="EXACT", evidence_before_wait="A019_REQUEST_DURABLE",
                    presentation_order=a["presentation_order"], request_evidence=exported[0]["human_evidence"], duplicate_requests=0)

    def test_ts5_06_explicit_resume_once(self):
        q = request(); a = self.child(q); b = self.child(answer(q, a))
        self.assertEqual((b["status"], b["continuation_count"]), ("RESPONSE_DURABLE", 0))
        self.assertEqual(self.child(observe(q))["continuation_count"], 0)
        c = self.child(resume(q, a))
        self.assertEqual((c["status"], c["continuation_count"], c["continuations_this_call"], c["terminal_count"]), ("STOP", 1, 1, 1))
        for op in (observe(q), resume(q, a), answer(q, a), resume(q, a, "human_cancel")):
            r = self.child(op)
            self.assertEqual((r["continuation_count"], r["continuations_this_call"], r["terminal_count"]), (1, 0, 1))
        # Reusing one public TestPort must also report per-call counts correctly.
        with OwnedHomeTestPort(self.store, scope=Scope(**SCOPE), human_owners=[OwnerFixture(**q["human_owners"][0])]) as port:
            for _ in range(2):
                self.assertEqual(port.execute({"op": "human_resume", "human_request_id": "human-1",
                                              "request_fingerprint": a["human_request"]["request_fingerprint"]})["continuations_this_call"], 0)
        self.passed("TS5-06", response_durable_before_resume=True, policy_version=c["executed_decision"]["policy_version"],
                    policy_fingerprint=c["executed_decision"]["policy_fingerprint"],
                    decision_fingerprint=c["executed_decision"]["decision_fingerprint"], continuation_count=1,
                    repeat_increment=0, terminal_count=1, public_sample=c)

    def test_ts5_07_crash_recovery(self):
        q = request(); faults = self.call(operation(q, "info"))["human_fault_points"]
        matrix = {}
        for fault in faults:
            path = self.root / fault
            if "REQUEST_" in fault:
                crashed = process(path, q, fault)
                self.assertEqual(crashed.returncode, 86, crashed.stdout + crashed.stderr)
                a = self.child(observe(q), path)
            else:
                a = self.call(q, path)
                if "RESPONSE_" in fault:
                    crashed = process(path, answer(q, a), fault)
                else:
                    self.call(answer(q, a), path)
                    crashed = process(path, resume(q, a), fault)
                self.assertEqual(crashed.returncode, 86, crashed.stdout + crashed.stderr)
            observed = self.child(observe(q), path)
            self.assertEqual(observed["human_request"], a["human_request"])
            self.assertEqual(observed["continuations_this_call"], 0)
            if "REQUEST_" in fault:
                self.call(answer(q, a), path)
            result = self.child(resume(q, a), path)
            unknown = fault in {"HUMAN_AFTER_RESUME_INTENT", "HUMAN_AFTER_CONTINUATION"}
            self.assertEqual(result["continuation_count"], "UNKNOWN" if unknown else 1)
            self.assertEqual(result["status"], "UNKNOWN" if unknown else "STOP")
            self.assertEqual(result["continuation_upper_bound"], 1)
            for _ in range(2):
                repeat = self.child(resume(q, a), path)
                self.assertEqual((repeat["continuations_this_call"], repeat["terminal_count"]), (0, 1))
                self.assertEqual(repeat["continuation_count"], result["continuation_count"])
            exported = self.call(operation(q, "safe_export"), path)["events"]
            kinds = [e["human_evidence"]["kind"] for e in exported]
            self.assertEqual(len(kinds), len(set(kinds)))
            matrix[fault] = {"recovery": result["status"], "count": result["continuation_count"],
                             "upper_bound": 1, "duplicate_hops": 0, "assistant_terminals": 1, "evidence_kinds": kinds}
        self.passed("TS5-07", crash_matrix=matrix, process_exit=86, automatic_resume=0)

    def test_ts5_08_gate_cannot_escalate(self):
        held = self.call(tool_request(3))
        self.assertEqual((held["status"], held["tool_executions"]), ("REQUIRE_HUMAN", 0))
        q = request(task_id="task-s4"); a = self.call(q)
        for field in ("model_text", "tool_output", "permission", "human_override", "authority_mutation", "break_glass"):
            bad = answer(q, a); bad["human_response"][field] = "ALLOW"
            self.rejected(bad)
        for text in ("ALLOW", "send", "P4 approved", "write_authority", "private RAW canary"):
            self.rejected(answer(q, a, text), "INVALID_PUBLIC_COMMAND")
        self.call(answer(q, a)); self.call(resume(q, a))
        r = self.call(tool_operation(tool_request(3), "tool_resume", action_id="action-1"))
        self.assertEqual((r["status"], r["tool_executions"]), ("REQUIRE_HUMAN", 0))
        for params in ({"operation": "critical"}, {"operation": "break_glass"}):
            critical = self.call(tool_request(4, parameters=params), self.root / params["operation"])
            self.assertEqual(critical["tool_executions"], 0)
        self.passed("TS5-08", held_action_after_human_resume="REQUIRE_HUMAN", p3_p4_executions=0,
                    model_ui_tool_overrides="DENY", authority_grants=False)

    def test_ts5_09_bounded_goal(self):
        q = request(); a = self.call(q); self.call(answer(q, a)); r = self.call(resume(q, a))
        self.assertEqual((r["continuation"]["goal_id"], r["continuation"]["task_id"]), ("goal-1", "task-1"))
        self.assertEqual(r["budget"]["limit"]["max_steps"], 1)
        self.assertEqual(r["budget"]["used"], {"steps": 1, "turns": 1, "tokens": 8, "time_ms": 1})
        self.assertEqual(r["budget"]["remaining"]["steps"], 0)
        self.assertEqual(r["stop_reason"], "ONE_HOP_COMPLETE")
        for _ in range(4):
            self.assertEqual(self.call(resume(q, a))["continuations_this_call"], 0)
        self.passed("TS5-09", continuation=r["continuation"], budget=r["budget"], continuation_count=1, stop="ONE_HOP_COMPLETE")

    def test_ts5_10_budget_and_unknown(self):
        budget = {"max_steps": 1, "max_turns": 1, "max_tokens": 64, "max_time_ms": 1000}
        cases = {}
        for key in budget:
            q = request(budget=budget | {key: 0}); path = self.root / key
            a = self.call(q, path); self.call(answer(q, a), path); r = self.call(resume(q, a), path)
            self.assertEqual((r["resume_decision"]["reason"], r["continuation_count"]), ("BUDGET_EXHAUSTED", 0))
            cases[key] = r["resume_decision"]["decision"]
        self.rejected(request(budget=budget | {"max_steps": 2}), "INVALID_CONTINUATION_BUDGET")
        q = request(); a = self.call(q)
        self.assertEqual(self.call(resume(q, a))["resume_decision"]["reason"], "MISSING_RESPONSE")
        for n, text in enumerate(("", "UNSURE", "HOLD", "CANCEL"), 2):
            qn = request(n); an = self.call(qn); self.call(answer(qn, an, text)); r = self.call(resume(qn, an))
            self.assertEqual(r["continuation_count"], 0)
            self.assertIn(r["resume_decision"]["decision"], ("HOLD", "CANCEL"))
        qclock = request(10); ac = self.call(qclock | {"human_now": "2026-09-18T01:00:00+00:00"})
        clock = self.call(observe(qclock))
        self.assertEqual((clock["status"], clock["stop_reason"], clock["continuation_count"]), ("UNKNOWN", "CLOCK_ROLLBACK", 0))
        self.passed("TS5-10", exhausted=cases, missing_ambiguous_resume=0, clock_rollback="UNKNOWN", guessed_input=0)

    def test_ts5_11_owner_binding(self):
        q = request(); variants = [[], q["human_owners"] * 2, q["human_owners"] + [dict(SCOPE, owner_id="speaker-2")]]
        for n, owners in enumerate(variants):
            path = self.root / str(n); changed = q | {"human_owners": owners}
            for _ in range(2):
                r = self.call(changed, path)
                self.assertEqual((r["status"], r["human_requests_created"]), ("HOLD", 0))
                self.assertIsNone(r["human_request"])
            self.assertEqual(self.call(operation(changed, "safe_export"), path)["events"], [])
        a = self.call(q); self.call(answer(q, a))
        for owners in variants:
            r = self.call(resume(q, a) | {"human_owners": owners})
            self.assertEqual((r["status"], r["continuation_count"]), ("HOLD", 0))
            self.assertIsNone(r["human_request"])
        self.assertEqual(self.call(q)["human_requests_created"], 0)
        self.assertEqual(self.call(resume(q, a))["continuation_count"], 1)
        self.passed("TS5-11", unique_owner=a["owner"], ambiguous_variants=3, duplicate_user_facing_requests=0,
                    revoked_owner_resume_count=0, persona_is_access_subject=False)

    def test_ts5_12_wake_gate(self):
        q = request(); candidate = dict(SCOPE, event_id="wake-1", owner_subject_id=SCOPE["access_subject_id"],
                                       observed_at="2026-09-18T00:00:00+00:00")
        denied = {"salience": 0, "urgency": 0, "confidence": 0, "quiet_hours": True, "cooldown_remaining": 1,
                  "repeat_count": 1, "owner_subject_id": "other", "universe_id": "other", "access_subject_id": "other"}
        for field, value in denied.items():
            path = self.root / field
            r = self.call(q | {"op": "human_wake", "candidate": candidate | {field: value}}, path)
            self.assertEqual((r["status"], r["human_requests_created"]), ("SILENT", 0))
            self.assertEqual(self.call(operation(q, "safe_export"), path)["events"], [])
        r = self.call(q | {"op": "human_wake", "candidate": candidate})
        self.assertEqual(r["wake_gate"]["decision"], "REQUEST_HUMAN")
        replay = self.child(q | {"op": "human_wake", "candidate": candidate})
        self.assertEqual(replay["human_requests_created"], 0)
        self.rejected(request(2) | {"op": "human_wake", "candidate": candidate}, "WAKE_IDENTITY_CONFLICT")
        self.assertEqual((r["notifications"], r["continuation_count"]), (0, 0))
        self.passed("TS5-12", wake_gate=r["wake_gate"], denied_variants=len(denied), duplicate_requests=0,
                    external_notifications=0, continuation_count=0)

    def test_ts5_13_evidence_authority(self):
        q = request(); a = self.call(q); b = self.call(answer(q, a)); r = self.call(resume(q, a))
        events = self.call(operation(q, "safe_export"))["events"]
        self.assertEqual([e["human_evidence"]["kind"] for e in events], ["REQUEST", "RESPONSE", "RESUME_INTENT", "CONTINUATION", "TERMINAL"])
        self.assertEqual(sum(e["actor_role"] == "assistant" for e in events), 1)
        self.assertEqual(b["human_response"]["normalized"]["source_raw_fingerprint"], b["human_response"]["raw_payload_fingerprint"])
        self.assertNotEqual(b["human_response"]["raw_payload_fingerprint"], b["human_response"]["normalized_fingerprint"])
        self.assertNotIn(" CONTINUE ", encode(events)); self.assertNotIn("raw_response", encode(events))
        self.assertFalse(r["authority"]); self.assertTrue(r["mutable_execution_control_only"])
        self.assertEqual(r["canonical_evidence_owner"], "A019")
        self.assertEqual(r["authority_mutation_count"], 0)
        for op in ("write_current", "write_agenda", "write_persona", "write_relationship", "write_canon", "human_control_table"):
            self.rejected(operation(q, op), "OPERATION_NOT_IN_SLICE")
        self.passed("TS5-13", canonical_evidence_owner="A019", evidence_events=5, second_journal=False,
                    authority_mutation_count=0, raw_vs_derived="SEPARATE_FINGERPRINTS_AND_PROVENANCE")

    def test_ts5_14_safe_trace_and_shell(self):
        q = request(); a = self.call(q); self.call(answer(q, a)); r = self.call(resume(q, a))
        trace = r["trace"]
        self.assertEqual(trace["trace_fingerprint"], digest({k: v for k, v in trace.items() if k != "trace_fingerprint"}))
        self.assertEqual(trace["identity"], {k: a["human_request"][k] for k in IDS})
        for field in ("request_state", "resume_decision", "budget", "owner", "recovery_outcome", "raw_payload_fingerprint", "normalized_fingerprint"):
            self.assertIn(field, trace)
        for canary in (" CONTINUE ", "journal.sqlite3", "state.json", "hidden_reasoning", "raw_response"):
            self.assertNotIn(canary, encode(trace))
        for n, bad in enumerate((request(owner_id="sk-SYNTHETICCANARY123"), request(public_safe=False), request(synthetic=False))):
            path = self.root / f"unsafe-{n}"; error = self.rejected(bad, store=path)
            self.assertNotIn("CANARY", error); self.assertFalse(path.exists())
        for raw in ("secret=CANARY", "Bearer CANARY123", "private RAW canary", "external body", "hidden_reasoning"):
            self.rejected(answer(q, a, raw))
        browser = self.shell_proof()
        self.passed("TS5-14", trace=trace, secret_private_raw_body_leaks=0, browser=browser,
                    safe_export_body_count=0, real_credential_reads=0)

    def shell_proof(self):
        server = make_server(self.root / "shell")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start(); self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        origin = "http://127.0.0.1:" + str(server.server_port)
        def http(method, path, body=None, headers=None):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request(method, path, body, headers or {})
            response = connection.getresponse(); status, data = response.status, response.read().decode(); connection.close()
            return status, data
        status, script = http("GET", "/human.js"); self.assertEqual(status, 200)
        self.assertIn('/human.js', http("GET", "/human")[1])
        harness = BROWSER_HARNESS.replace("'owned-home-v1:'", "'owned-home-v5:'").replace(
            "'form','message','submit','resume','next','status','reply'",
            "'form','message','submit','resume','next','status','reply','create','cancel'")
        def browser(steps, storage=None):
            run = subprocess.run(["node", "-e", harness], input=encode({"origin": origin, "script": script, "steps": steps, "storage": storage or {}}),
                                 text=True, capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            for call in result["calls"]:
                if call.get("data"):
                    self.assertTrue(call["data"]["ok"], call)
            for key, value in result["writes"]:
                self.assertEqual(key, result["key"])
                self.assertEqual(set(json.loads(value)), {"id", "created_at", "expires_at"})
                for forbidden in ("CONTINUE", "response", "trace", "receipt", "CANARY", "raw"):
                    self.assertNotIn(forbidden, value)
            return result
        result = browser([{"event": "create"}, {"event": "reload"}, {"event": "submit", "text": " CONTINUE "},
                          {"event": "reload"}, {"event": "resume"}, {"event": "reload"}])
        self.assertEqual(result["states"][-1]["status"], "ONE_HOP_COMPLETE")
        self.assertEqual([c["data"]["result"]["continuations_this_call"] for c in result["calls"]], [0, 0, 0, 0, 1, 0])
        for loss in ("before", "after"):
            r = browser([{"event": "create"}, {"event": "submit", "text": "CONTINUE", "drop": loss}, {"event": "reload"},
                         {"event": "submit", "text": "CONTINUE"}, {"event": "resume"}, {"event": "reload"}])
            self.assertEqual(r["states"][-1]["status"], "ONE_HOP_COMPLETE")
            r = browser([{"event": "create"}, {"event": "submit", "text": "CONTINUE"}, {"event": "resume", "drop": loss},
                         {"event": "reload"}, {"event": "resume"}, {"event": "reload"}])
            self.assertEqual(r["states"][-1]["status"], "ONE_HOP_COMPLETE")
            self.assertEqual(sum(c["data"]["result"]["continuations_this_call"] for c in r["calls"] if c.get("data")), 1)
            r = browser([{"event": "create", "drop": loss}, {"event": "reload"}, {"event": "create"}, {"event": "reload"}])
            created = [c["data"]["result"] for c in r["calls"] if c.get("data") and c["body"]["op"] == "human_request"]
            self.assertEqual(len(created), 1)
            self.assertEqual(r["states"][-1]["status"], "WAITING")
        # Legacy/poisoned storage is rewritten to identity-only before observation.
        saved = result["states"][-1]["storage"]; key = result["key"]
        poison = json.loads(saved[key]); poison.update(raw="CANARY", response="CONTINUE")
        clean = browser([{"event": "inspect"}], {key: encode(poison)})
        self.assertNotIn("CANARY", encode(clean["writes"]))
        body = result["calls"][0]["body"] | {"human_owners": [dict(SCOPE, owner_id="forged")], "human_override": True}
        status, denied = http("POST", "/v1/human", encode(body), {"Content-Type": "application/json", "X-Owned-Home": "1"})
        self.assertEqual(status, 400); self.assertFalse(json.loads(denied)["ok"])
        status, _ = http("POST", "/v1/human", encode(result["calls"][0]["body"]),
                         {"Content-Type": "application/json", "X-Owned-Home": "1", "Origin": "https://evil.invalid"})
        self.assertEqual(status, 403)
        return {"engine": "EXACT_SERVED_JS_NODE_VM_REAL_LOOPBACK_HTTP", "transport_boundaries": 6,
                "persisted_control_fields": 3, "persisted_response_bodies": 0, "duplicate_hops": 0,
                "ui_owner_override": "DENY", "cross_origin": "DENY"}

    def test_ts5_15_regression_blackbox(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK_FORBIDDEN")), \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS_FORBIDDEN")):
            q = request(); a = self.call(q); self.call(answer(q, a)); r = self.call(resume(q, a))
            for key in ("notifications", "authority_mutation_count", "real_credential_reads", "real_external_side_effects", "automatic_resumes"):
                self.assertEqual(r[key], 0)
        self.assertEqual(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"], [])
        run = subprocess.run([sys.executable, "tests/test_owned_home_slice4_conformance.py", "--receipt"],
                             cwd=ROOT, text=True, capture_output=True, timeout=180)
        self.assertEqual(run.returncode, 0, run.stderr)
        regression = json.loads(run.stdout)
        self.assertEqual((regression["tests_run"], regression["failures"], regression["errors"]), (15, 0, 0))
        self.assertEqual(regression["conformance_fingerprint"], PRIOR["TS4"])
        self.assertEqual(regression["matrix"]["TS4-14"]["unchanged_fingerprints"], {k: v for k, v in PRIOR.items() if k != "TS4"})
        self.passed("TS5-15", unchanged_fingerprints=PRIOR, regression_counts={"TS4": 15, "TS3": 15, "TS2": 15, "TS1": 12, "A019": 21},
                    sanctioned_oracles=["OwnedHomeTestPort v1", "CLI JSON", "loopback HTTP"], internal_oracles=0,
                    network_guard="CONNECT_AND_DNS_DENIED", runtime_dependencies=[], real_credentials=0, real_connectors=0, spend=0)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Slice5Conformance))
    if "--receipt" in sys.argv:
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
        receipt = {"work_order": "WO-A1-A029-P2S5-01 v0.1", "S0_BASE_SHA": BASE_SHA, "S0_BASE_TREE": BASE_TREE,
                   "head_sha": git("rev-parse", "HEAD"), "head_tree": git("rev-parse", "HEAD^{tree}"),
                   "branch": git("branch", "--show-current"), "working_tree_clean": not bool(git("status", "--porcelain")),
                   "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                   "node": subprocess.check_output(["node", "--version"], text=True).strip(), "runtime_dependencies": [],
                   "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                   "matrix": MATRIX, "conformance_fingerprint": digest(MATRIX),
                   "zero_tolerance_count": 0 if result.wasSuccessful() and len(MATRIX) == 15 else "NOT_EVALUABLE",
                   "status": "READY_FOR_INDEPENDENT_A2_SLICE5_REVIEW / A1 STOP" if result.wasSuccessful() and len(MATRIX) == 15 else "REPAIR_REQUIRED",
                   "limitations": ["Public synthetic commands and one fixed-cost local hop only; no P3/P4 human override",
                                   "Process crash proof, not physical power-cut proof", "Served JS Node VM, not native browser rendering proof",
                                   "No A2 verdict, merge, full-P2, release or next-stage authorization"]}
        print(encode(receipt))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
