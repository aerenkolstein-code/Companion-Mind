"""WO-A1-A029-P2S4-01 public TestPort/CLI/loopback conformance.

Run: python tests/test_owned_home_slice4_conformance.py --receipt
CLI: python -m companion_mind.owned_home.testport --store DIR [--fault POINT].
Requests retain contract_version=owned-home/1 and scope. Trusted synthetic setup:
skill_contracts (optional exact registry), tool_targets and tool_grants. Operations:
skill_registry, skill_lookup(skill_id,skill_version), action_preview(action),
permission_evaluate(action), tool_execute(action,expected_intent?,expected_decision?),
tool_observe(action_id), tool_resume(action_id). Fixtures cannot redefine tiers.
Action fields are demonstrated in request() below. There is no live connector,
credential payload, free-form tool body or HumanResponse override. Numeric values
are explicitly public synthetic data. TestPort grants are trusted test setup;
the /tools browser cannot supply grants or contracts. Same action/key replays its
receipt. A changed action/key binding fails closed. All recovery is observational
except an explicit pre-dispatch resume or receipt-only P2 readback reconciliation.
Tests never open internal control files, indexes or journal tables. Source/Git
inspection is confined to the required repository scope/dependency gate.
"""
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
from test_owned_home_slice2_conformance import SCOPE, encode, digest, request as context_request

BASE_SHA = "6a4798ad81145dd4606923e2f2e1b1e3f3e088ec"
BASE_TREE = "dff89b764eabcb8d1833256a9540edf88e19b536"
PRIOR = {"TS3": "a0ec993b577fca1565c9104ac03a3808857b82bdec234f688c11d4530629c58f",
         "TS2": "05ac94539f2b7be4829e3a3405b9f053f21f1c2ba2e3fcff4fc81bb4293f5999",
         "TS1": "5b8dd9100d3ec7d0370a7ac489499b2a10677d0db0a07ce80575445a32678be1",
         "A019": "d20d06a356051f71972ef1f5aa34d1dc72eddb8f70b74773ffb0a535ed1cfa6d"}
SKILLS = ("synthetic.compute", "synthetic.scoped_read", "synthetic.reversible_write",
          "synthetic.consequential_send", "synthetic.critical")
SKILL_FIELDS = ("skill_id", "skill_version", "permission_tier", "side_effect_class", "connector_class",
                "retry_budget", "loop_budget", "synthetic", "public_safe")
MATRIX = {}


def request(tier=0, number=1, **changes):
    parameters = ({"values": [2, 3]} if tier == 0 else {"value": 7} if tier == 2 else
                  {"operation": "critical"} if tier == 4 else {})
    action = dict(SCOPE, action_id=f"action-{number}", task_id="task-s4", request_id=f"req-{number}",
                  session_id="session-s4", turn_id=f"turn-{number}", turn_no=number,
                  skill_id=SKILLS[tier], skill_version="v1", target_id="target-1",
                  idempotency_key=f"key-{number}", parameters=parameters, grant_id=None if tier == 0 else "grant-1")
    action.update(changes)
    return {"contract_version": "owned-home/1", "scope": dict(SCOPE), "op": "tool_execute", "action": action,
            "tool_targets": [dict(SCOPE, target_id="target-1", initial_value=0)],
            "tool_grants": [dict(SCOPE, grant_id="grant-1", resource_ids=["target-1"], operations=["read", "write"])]}


def operation(req, op, **fields):
    return {k: deepcopy(v) for k, v in req.items() if k in (
        "contract_version", "scope", "fixtures", "grants", "skill_contracts", "tool_targets", "tool_grants")} | {"op": op, **fields}


def process(store, req, fault=None):
    command = [sys.executable, "-m", "companion_mind.owned_home.testport", "--store", str(store)]
    if fault:
        command += ["--fault", fault]
    return subprocess.run(command, input=encode(req), text=True, capture_output=True, cwd=ROOT, timeout=25)


class Slice4Conformance(unittest.TestCase):
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

    def rejected(self, req, store=None, expected=None):
        result = process(store or self.store, req)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        error = json.loads(result.stdout)["error"]
        if expected:
            self.assertEqual(error, expected)
        return error

    def preview(self, req):
        return self.call(req | {"op": "action_preview"})

    def passed(self, case, **facts):
        MATRIX[case] = {"status": "PASS", **facts}

    def test_ts4_01_registry_determinism(self):
        registry = self.call(operation(request(), "skill_registry"))
        fixtures = [{k: p[k] for k in SKILL_FIELDS} for p in registry["skills"]]
        for ordering in itertools.permutations(fixtures):
            self.assertEqual(self.call(operation(request(), "skill_registry") | {"skill_contracts": list(ordering)}), registry)
        self.assertEqual(registry["registry_fingerprint"], digest(registry["skills"]))
        self.assertEqual(len(registry["skills"]), 5)
        for skill in registry["skills"]:
            self.assertEqual(skill["skill_fingerprint"], digest({k: v for k, v in skill.items() if k != "skill_fingerprint"}))
        duplicate = operation(request(), "skill_registry") | {"skill_contracts": [fixtures[0], fixtures[0]]}
        self.rejected(duplicate, self.root / "duplicate", "DUPLICATE_SKILL_IDENTITY")
        self.assertFalse((self.root / "duplicate").exists())
        self.passed("TS4-01", permutations=120, registry_fingerprint=registry["registry_fingerprint"],
                    skills=[{k: s[k] for k in ("skill_id", "skill_version", "skill_fingerprint")} for s in registry["skills"]])

    def test_ts4_02_exact_binding(self):
        for skill, version in ((SKILLS[1], "v0"), ("synthetic.missing", "v1")):
            q = request(1, skill_id=skill, skill_version=version)
            lookup = self.call(operation(q, "skill_lookup", skill_id=skill, skill_version=version))
            self.assertEqual(lookup["status"], "EXACT_SKILL_UNRESOLVED")
            result = self.call(q, self.root / version)
            self.assertEqual((result["status"], result["tool_executions"]), ("DENY", 0))
        q = request(2); preview = self.preview(q)
        intent, decision = preview["action_intent"], preview["permission_decision"]
        self.assertEqual(intent["intent_fingerprint"], digest({k: v for k, v in intent.items() if k != "intent_fingerprint"}))
        self.assertEqual(decision["decision_fingerprint"], digest({k: v for k, v in decision.items() if k != "decision_fingerprint"}))
        changes = [{"parameters": {"value": 8}}, {"target_id": "target-2"}, {"task_id": "task-other"},
                   {"request_id": "request-other"}, {"turn_id": "turn-other"}, {"action_id": "action-other"},
                   {"idempotency_key": "key-other"}, {"session_id": "session-other"}, {"skill_version": "v2"},
                   {"reversible": False}, {"expected_readback": "NONE"}, {"declared_tier": "P0_PURE"}]
        for n, change in enumerate(changes):
            changed = request(2, **change)
            fresh = self.preview(changed)
            self.assertNotEqual(fresh["action_intent"]["intent_fingerprint"], intent["intent_fingerprint"])
            result = self.call(changed | {"expected_intent": intent, "expected_decision": decision}, self.root / f"drift-{n}")
            self.assertEqual(result["permission_decision"]["decision"], "DENY")
            self.assertEqual(result["tool_executions"], 0)
        revoked = deepcopy(q); revoked["tool_grants"][0]["revoked"] = True
        self.assertNotEqual(self.preview(revoked)["permission_decision"]["decision_fingerprint"], decision["decision_fingerprint"])
        forged = deepcopy(decision); forged["policy_version"] = "stale-policy"
        result = self.call(q | {"expected_decision": forged}, self.root / "policy-drift")
        self.assertEqual((result["status"], result["tool_executions"]), ("DENY", 0))
        self.passed("TS4-02", action_drift_variants=len(changes), stale_grant_policy="DENY",
                    action_fingerprint=intent["intent_fingerprint"], decision_fingerprint=decision["decision_fingerprint"],
                    policy=preview["policy"], grant_fingerprint=decision["grant_ref"]["grant_fingerprint"])

    def test_ts4_03_pure(self):
        q = request(); q["tool_grants"] = []; q["tool_targets"] = []
        a = self.call(q); b = self.call(q, self.root / "same")
        self.assertEqual((a["status"], a["tool_executions"], a["tool_receipt"]["pure_result"]), ("SUCCESS", 1, 5))
        self.assertEqual(a["tool_receipt"], b["tool_receipt"])
        for field in ("synthetic_reads", "synthetic_writes", "authority_mutation_count", "real_external_side_effects"):
            self.assertEqual(a["tool_receipt"][field], 0)
        self.assertIsNone(a["permission_decision"]["grant_ref"])
        self.passed("TS4-03", deterministic_result=5, grant_required=False, external_and_authority_effects=0)

    def test_ts4_04_scoped_read(self):
        q = request(1); result = self.call(q)
        self.assertEqual((result["status"], result["tool_executions"]), ("SUCCESS", 1))
        self.assertEqual((result["synthetic_target"]["reads"], result["synthetic_target"]["writes"]), (1, 0))
        changes = [{"universe_id": "other"}, {"access_subject_id": "other"}, {"resource_ids": ["other"]},
                   {"operations": ["write"]}, {"revoked": True}, {"validity": "EXPIRED"},
                   {"validity": "NOT_YET_VALID"}, {"credential_domain_id": "other"}]
        for n, change in enumerate(changes):
            bad = deepcopy(q); bad["tool_grants"][0].update(change)
            r = self.call(bad, self.root / str(n))
            self.assertEqual((r["status"], r["tool_executions"]), ("DENY", 0))
            self.assertIsNone(r["tool_receipt"])
        denied = self.call(q | {"tool_grants": []}, self.root / "missing")
        self.assertEqual((denied["status"], denied["tool_executions"]), ("DENY", 0))
        self.passed("TS4-04", allowed_reads=1, denied_scope_variants=9, denied_reads=0)

    def test_ts4_05_reversible_readback(self):
        q = request(2); preview = self.preview(q)
        r = self.call(q | {"expected_intent": preview["action_intent"], "expected_decision": preview["permission_decision"]})
        self.assertEqual((r["status"], r["tool_executions"]), ("SUCCESS", 1))
        self.assertEqual(r["tool_receipt"]["readback"], {"status": "VERIFIED", "observed_fingerprint": digest(7)})
        self.assertEqual(r["synthetic_target"]["writes"], 1)
        self.assertTrue(all(r["action_control"]["milestones"].values()))
        self.assertEqual(r["presentation_order"][-2:], ["A019_TERMINAL_DURABLE", "DISPLAY"])
        for mode in ("MISMATCH", "UNAVAILABLE"):
            failed = self.call(request(2, readback_mode=mode), self.root / mode)
            self.assertEqual(failed["status"], "UNKNOWN")
            self.assertEqual(failed["tool_receipt"]["readback"]["status"], mode)
            self.assertEqual(failed["synthetic_target"]["writes"], 1)
        for n, change in enumerate(({"reversible": False}, {"expected_readback": "NONE"}, {"grant_id": None})):
            denied = self.call(request(2, **change), self.root / f"denied-{n}")
            self.assertEqual((denied["status"], denied["tool_executions"]), ("DENY", 0))
        self.passed("TS4-05", writes=1, readback="VERIFIED", receipt=r["tool_receipt"],
                    lifecycle=r["action_control"], unverified_successes=0, authority_mutations=0)

    def test_ts4_06_consequential_hold(self):
        r = self.call(request(3))
        self.assertEqual((r["status"], r["tool_executions"]), ("REQUIRE_HUMAN", 0))
        self.assertIsNone(r["tool_receipt"])
        self.assertFalse(r["action_control"]["milestones"]["DISPATCH_INTENT_DURABLE"])
        again = self.child(operation(request(3), "tool_resume", action_id="action-1"))
        self.assertEqual((again["status"], again["tool_executions"]), ("REQUIRE_HUMAN", 0))
        self.passed("TS4-06", decision="REQUIRE_HUMAN", executions=0, sends=0, human_override=False)

    def test_ts4_07_critical_hold_deny(self):
        cases = ("critical", "break_glass", "credential_mutation", "grant_mutation", "security_reconfiguration", "destructive_delete")
        results = {}
        for op in cases:
            r = self.call(request(4, parameters={"operation": op}), self.root / op)
            self.assertEqual(r["status"], "REQUIRE_HUMAN" if op == "critical" else "DENY")
            self.assertEqual(r["tool_executions"], 0)
            self.assertIsNone(r["tool_receipt"])
            results[op] = r["status"]
        self.passed("TS4-07", decisions=results, executions=0)

    def test_ts4_08_escalation(self):
        for n, change in enumerate(({"declared_tier": "P0_PURE"}, {"declared_side_effect": "NONE"})):
            r = self.call(request(3, **change), self.root / f"declared-{n}")
            self.assertEqual((r["status"], r["tool_executions"]), ("DENY", 0))
        for n, field in enumerate(("model_text", "confidence", "human_confirmation", "tool_output", "authority_mutation")):
            bad = request(3); bad["action"][field] = "approved"
            self.rejected(bad, self.root / f"injected-{n}", "INVALID_REQUEST")
        bad = operation(request(), "skill_registry") | {"skill_contracts": [
            {"skill_id": SKILLS[3], "permission_tier": "P0_PURE", "side_effect_class": "NONE"}]}
        self.rejected(bad, self.root / "redefine", "SKILL_TIER_REDEFINITION")
        forged = self.preview(request(3))["permission_decision"]
        forged.update(decision="ALLOW", execution_allowed=True)
        r = self.call(request(3) | {"expected_decision": forged}, self.root / "forged")
        self.assertEqual((r["status"], r["tool_executions"]), ("DENY", 0))
        self.passed("TS4-08", escalation_successes=0, fixture_tier_redefinition="DENY", model_ui_override=False)

    def test_ts4_09_outcomes(self):
        receipts = {}
        for outcome in ("SUCCESS", "FAILED", "PARTIAL", "UNKNOWN"):
            r = self.call(request(2, script=outcome), self.root / outcome)
            self.assertEqual(r["status"], outcome)
            self.assertEqual(r["tool_receipt"]["outcome"], outcome)
            self.assertEqual(r["tool_trace"]["terminal_outcome"], outcome)
            self.assertEqual(r["tool_receipt"]["execution_count"], 1)
            self.assertEqual(r["synthetic_target"]["writes"], int(outcome != "FAILED"))
            self.assertEqual(r["canonical_evidence"]["outcome"], outcome)
            receipts[outcome] = r["tool_receipt"]["receipt_fingerprint"]
        self.assertEqual(len(set(receipts.values())), 4)
        self.passed("TS4-09", distinct_outcomes=receipts, unknown_promotions=0)

    def test_ts4_10_crash_recovery(self):
        faults = self.call(operation(request(), "info"))["tool_fault_points"]
        results = {}
        for fault in faults:
            path = self.root / fault; q = request(2)
            crashed = process(path, q, fault)
            self.assertEqual(crashed.returncode, 86, crashed.stdout + crashed.stderr)
            seen = self.child(operation(q, "tool_observe", action_id="action-1"), path)
            self.assertEqual(seen["tool_executions"], 0)
            pre = fault in ("TOOL_AFTER_USER_DURABLE", "TOOL_AFTER_PERMISSION")
            ambiguous = fault in ("TOOL_AFTER_DISPATCH_INTENT", "TOOL_AFTER_EFFECT")
            if pre:
                self.assertEqual(seen["status"], "AWAIT_EXPLICIT_RESUME")
            elif ambiguous:
                self.assertEqual(seen["status"], "UNKNOWN")
                self.assertEqual(seen["tool_receipt"]["execution_count"], "UNKNOWN")
                self.assertEqual(seen["tool_receipt"]["execution_upper_bound"], 1)
            elif fault == "TOOL_AFTER_RECEIPT":
                self.assertEqual(seen["status"], "AWAIT_EXPLICIT_RECONCILE")
                self.assertIsNone(seen["visible_reply"])
                self.assertEqual(seen["terminal_count"], 0)
            else:
                self.assertEqual(seen["status"], "SUCCESS")
            resumed = self.child(operation(q, "tool_resume", action_id="action-1"), path)
            self.assertEqual(resumed["tool_executions"], int(pre))
            self.assertEqual(resumed["status"], "UNKNOWN" if ambiguous else "SUCCESS")
            self.assertEqual(resumed["synthetic_target"]["writes"], int(fault != "TOOL_AFTER_DISPATCH_INTENT"))
            again = self.child(operation(q, "tool_resume", action_id="action-1"), path)
            self.assertEqual(again["tool_executions"], 0)
            self.assertEqual(again["tool_receipt"], resumed["tool_receipt"])
            self.assertEqual((again["event_count"], again["terminal_count"]), (2, 1))
            results[fault] = {"observed": seen["status"], "resumed": resumed["status"],
                              "resume_executions": resumed["tool_executions"], "total_writes": again["synthetic_target"]["writes"]}
        # Permission can disappear while a dispatch is pending. Resume must not
        # reuse the previously ALLOWed decision. Post-receipt revocation cannot
        # obtain a readback or turn UNKNOWN into SUCCESS either.
        for fault in ("TOOL_AFTER_PERMISSION", "TOOL_AFTER_RECEIPT"):
            path = self.root / (fault + "-revoked"); q = request(2)
            self.assertEqual(process(path, q, fault).returncode, 86)
            q["tool_grants"][0]["revoked"] = True
            result = self.child(operation(q, "tool_resume", action_id="action-1"), path)
            self.assertEqual(result["permission_decision"]["decision"], "DENY")
            self.assertEqual(result["tool_executions"], 0)
            self.assertNotEqual(result["status"], "SUCCESS")
        self.passed("TS4-10", process_crashes=results, automatic_redispatches=0, duplicate_effects=0,
                    revoked_resume_executions=0, physical_power_cut_proof=False)

    def test_ts4_11_replay_and_conflict(self):
        q = request(2); first = self.call(q)
        for op in ("tool_execute", "tool_observe", "tool_resume"):
            repeated = q if op == "tool_execute" else operation(q, op, action_id="action-1")
            result = self.child(repeated)
            self.assertEqual(result["tool_receipt"], first["tool_receipt"])
            self.assertEqual(result["tool_executions"], 0)
            self.assertEqual(result["synthetic_target"]["writes"], 1)
        for change in ({"parameters": {"value": 8}}, {"action_id": "other"}, {"target_id": "other"}):
            self.rejected(request(2, **change), expected="IDEMPOTENCY_CONFLICT")
        self.rejected(request(2, idempotency_key="other"), expected="ACTION_IDENTITY_CONFLICT")
        revoked = deepcopy(q); revoked["tool_grants"][0]["revoked"] = True
        r = self.child(operation(revoked, "tool_observe", action_id="action-1"))
        self.assertEqual((r["status"], r["tool_executions"]), ("PERMISSION_CONTEXT_CHANGED", 0))
        self.assertIsNone(r["tool_receipt"]); self.assertIsNone(r["visible_reply"])
        self.assertEqual(r["historical_receipt_fingerprint"], first["tool_receipt"]["receipt_fingerprint"])
        self.passed("TS4-11", duplicate_dispatches=0, total_writes=1, changed_key_binding="IDEMPOTENCY_CONFLICT",
                    reused_action_identity="ACTION_IDENTITY_CONFLICT", revoked_receipt_reuse="DENY")

    def test_ts4_12_authority_separation(self):
        before = self.call(context_request())
        r = self.call(request(2, request_id="tool-req-1", turn_id="tool-turn-1"))
        after = self.call(context_request(2))
        self.assertEqual(before["projection"]["working_set"]["snapshot"], after["projection"]["working_set"]["snapshot"])
        self.assertEqual(r["authority_mutation_count"], 0)
        self.assertFalse(r["action_control"]["authority"])
        self.assertTrue(r["action_control"]["mutable_execution_control_only"])
        self.assertEqual(r["action_control"]["canonical_evidence_owner"], "A019")
        events = self.call(operation(request(), "safe_export"))["events"]
        evidence = [e for e in events if e.get("tool_evidence")]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["tool_evidence"], r["canonical_evidence"])
        self.assertNotIn("parameters", encode(evidence))
        for op in ("mutate_current", "mutate_agenda", "mutate_persona", "mutate_relationship", "mutate_canon"):
            self.rejected(operation(request(), op), expected="OPERATION_NOT_IN_SLICE")
        self.passed("TS4-12", canonical_tool_evidence=1, authority_mutations=0, source_snapshot_unchanged=True,
                    execution_control_is_authority=False)

    def test_ts4_13_scope_secrets_trace(self):
        q = request(2, parameters={"value": 876543}); r = self.call(q)
        trace = r["tool_trace"]
        fields = ("skill_ref", "skill_fingerprint", "action_id", "action_fingerprint", "permission_tier",
                  "side_effect_class", "scope", "grant_ref", "permission_decision", "permission_fingerprint",
                  "dispatch_state", "milestones", "readback_status", "receipt_fingerprint", "terminal_outcome",
                  "retry_decision", "execution_count", "execution_upper_bound", "authority_mutation_count")
        self.assertTrue(all(field in trace for field in fields))
        self.assertEqual(trace["trace_fingerprint"], digest({k: v for k, v in trace.items() if k != "trace_fingerprint"}))
        wire = encode({k: r[k] for k in ("tool_trace", "tool_receipt", "canonical_evidence", "action_control")})
        for canary in ("876543", "parameters", "canonical_event_log", "journal.sqlite3", "hidden_reasoning", "raw_body"):
            self.assertNotIn(canary, wire)
        foreign = operation(q, "tool_observe", action_id="action-1"); foreign["scope"]["access_subject_id"] = "other"
        self.assertEqual(self.call(foreign)["status"], "NOT_FOUND")
        self.rejected(request(2, universe_id="other"), expected="SCOPE_DENIED")
        bads = [request(2, target_id="sk-SYNTHETICCANARY123"), request(2, parameters={"value": "secret=CANARY"}),
                request(2, public_safe=False), request(2, synthetic=False)]
        bad = request(2); bad["tool_grants"][0]["api_key"] = "SYNTHETIC_CANARY"; bads.append(bad)
        bad = request(2); bad["tool_targets"][0]["public_safe"] = False; bads.append(bad)
        for n, bad in enumerate(bads):
            path = self.root / f"unsafe-{n}"; error = self.rejected(bad, path)
            self.assertNotIn("CANARY", error); self.assertFalse(path.exists())
        self.passed("TS4-13", required_trace_fields=len(fields), secret_private_raw_body_leaks=0,
                    unsafe_inputs_denied=len(bads), cross_scope_receipts=0)

    def test_ts4_14_shell_and_regression(self):
        server = make_server(self.store)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start(); self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        origin = "http://127.0.0.1:" + str(server.server_port)
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/tools.js"); response = connection.getresponse()
        self.assertEqual(response.status, 200); script = response.read().decode(); connection.close()
        harness = BROWSER_HARNESS.replace("'owned-home-v1:'", "'owned-home-v4:'").replace(
            "'form','message','submit','resume','next','status','reply'",
            "'form','message','submit','resume','next','status','reply','skill','apply-skill','active-skill','tool-info'").replace(
            "if ('text' in step) elements.message.value = step.text;",
            "if ('text' in step) elements.message.value = step.text; if ('skill' in step) elements.skill.value=step.skill;")
        def browser(steps):
            run = subprocess.run(["node", "-e", harness], input=encode({"origin": origin, "script": script, "steps": steps}),
                                 text=True, capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(run.stdout)
        steps = [{"event": "submit", "text": "2,3"}, {"event": "reload"}]
        for tier in (1, 2, 3, 4):
            steps += [{"event": "next"}, {"event": "apply-skill", "skill": SKILLS[tier]},
                      {"event": "submit", "text": "876543" if tier == 2 else ""}, {"event": "reload"}]
        result = browser(steps)
        actions = [c for c in result["calls"] if c["body"]["op"] == "tool_execute"]
        self.assertEqual(len(actions), 5)
        self.assertTrue(all(c["data"]["ok"] for c in actions), actions)
        self.assertEqual([c["data"]["result"]["status"] for c in actions], ["SUCCESS"] * 3 + ["REQUIRE_HUMAN"] * 2)
        self.assertEqual([c["data"]["result"]["tool_executions"] for c in actions], [1, 1, 1, 0, 0])
        self.assertEqual(len({c["body"]["action"]["session_id"] for c in actions}), 1)
        self.assertEqual([c["body"]["action"]["turn_no"] for c in actions], list(range(1, 6)))
        for c in result["calls"]:
            if c["body"]["op"] == "tool_observe":
                self.assertEqual(c["data"]["result"]["tool_executions"], 0)
        for key, value in result["writes"]:
            self.assertEqual(key, result["key"])
            self.assertEqual(set(json.loads(value)), {"session_id", "action_id", "turn_no", "skill_id", "skill_version", "target_id"})
            self.assertNotIn("876543", value); self.assertNotIn("receipt", value)
        for loss in ("before", "after"):
            lost = browser([{"event": "submit", "text": "2,3", "drop": loss}, {"event": "reload"},
                            {"event": "submit", "text": "2,3"}, {"event": "reload"}])
            executions = [c for c in lost["calls"] if c["body"]["op"] == "tool_execute" and c.get("data")]
            self.assertEqual(len(executions), 1)
            self.assertEqual(executions[0]["data"]["result"]["tool_executions"], 1)
        body = {"contract_version": "owned-home/1", "op": "tool_execute", "action": actions[3]["body"]["action"],
                "tool_grants": request()["tool_grants"], "human_override": True}
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/v1/tool", encode(body), {"Content-Type": "application/json", "X-Owned-Home": "1"})
        response = connection.getresponse(); denied = json.loads(response.read()); connection.close()
        self.assertFalse(denied["ok"])
        # TS3-14 recursively runs unchanged TS2/TS1/A019 receipt matrices.
        run = subprocess.run([sys.executable, "tests/test_owned_home_slice3_conformance.py", "--receipt"],
                             cwd=ROOT, text=True, capture_output=True, timeout=150)
        self.assertEqual(run.returncode, 0, run.stderr)
        regression = json.loads(run.stdout)
        self.assertEqual((regression["tests_run"], regression["failures"], regression["errors"]), (15, 0, 0))
        self.assertEqual(regression["conformance_fingerprint"], PRIOR["TS3"])
        self.assertEqual(regression["matrix"]["TS3-14"]["unchanged_fingerprints"], {k: v for k, v in PRIOR.items() if k != "TS3"})
        self.passed("TS4-14", browser="EXACT_SERVED_JS_NODE_VM_REAL_LOOPBACK_HTTP", flows=5,
                    persisted_control_fields=6, raw_body_storage=0, transport_loss_duplicate_dispatches=0,
                    ui_grant_override="DENY", regression_counts={"TS3": 15, "TS2": 15, "TS1": 12, "A019": 21},
                    unchanged_fingerprints=PRIOR)

    def test_ts4_15_public_blackbox_network_scope(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK_FORBIDDEN")), \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS_FORBIDDEN")):
            for tier in range(5):
                q = request(tier)
                result = self.call(q, self.root / str(tier))
                for field in ("provider_invocations", "real_credential_reads", "real_external_side_effects", "spend", "authority_mutation_count"):
                    self.assertEqual(result[field], 0)
                self.assertEqual(result["automatic_redispatches"], 0)
        q = request(2); first = self.call(q)
        observed = self.child(operation(q, "tool_observe", action_id="action-1"))
        self.assertEqual(observed["tool_receipt"], first["tool_receipt"])
        self.assertEqual(observed["tool_trace"], first["tool_trace"])
        for op in ("query_db", "action_control_table", "tool_registry_object", "live_connector", "credential", "human_response"):
            self.rejected(operation(q, op), expected="OPERATION_NOT_IN_SLICE")
        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(config["project"]["dependencies"], [])
        # Source/Git inspection only for scope; behavioral assertions above use
        # public TestPort/CLI/HTTP receipts exclusively.
        baseline_blobs = {
            "tests/test_owned_home_slice2_conformance.py": "56e90aa6d23c8cd8aaf4cddf5fdd5a90c4a09f48",
            "tests/test_owned_home_slice3_conformance.py": "280235efb650fd359fec5a527b3db78f815c4371",
            "companion_mind/owned_home/context.py": "e9093fa6d49925e01b1743de255af6e1b2581e77",
            "companion_mind/owned_home/contracts.py": "5fecdebc9a25fb2511ab9d259e5293d1437255b7",
            "companion_mind/owned_home/model_gateway.py": "a87358cbbc89e5911bc47627ca946f4231dd7b1c",
            "companion_mind/owned_home/router.py": "27628c3a8118eb6ce7ee774eea28e4ce885efb89",
            "pyproject.toml": "573eeca8246815a015c7e303babb6eab6e571a9d"}
        for path, baseline in baseline_blobs.items():
            actual = subprocess.check_output(["git", "hash-object", path], cwd=ROOT, text=True).strip()
            self.assertEqual(actual, baseline, path)
        self.passed("TS4-15", sanctioned_oracles=["OwnedHomeTestPort v1", "CLI JSON", "loopback HTTP"],
                    internal_oracles=0, network_guard="CONNECT_AND_DNS_DENIED", runtime_dependencies=[],
                    real_credentials=0, real_connectors=0, spend=0, a2_verdict=False)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Slice4Conformance))
    if "--receipt" in sys.argv:
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
        receipt = {"work_order": "WO-A1-A029-P2S4-01", "S0_BASE_SHA": BASE_SHA, "S0_BASE_TREE": BASE_TREE,
                   "head_sha": git("rev-parse", "HEAD"), "head_tree": git("rev-parse", "HEAD^{tree}"),
                   "branch": git("branch", "--show-current"), "working_tree_clean": not bool(git("status", "--porcelain")),
                   "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                   "node": subprocess.check_output(["node", "--version"], text=True).strip(), "runtime_dependencies": [],
                   "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                   "matrix": MATRIX, "conformance_fingerprint": digest(MATRIX),
                   "zero_tolerance_count": 0 if result.wasSuccessful() and len(MATRIX) == 15 else "NOT_EVALUABLE",
                   "status": "READY_FOR_INDEPENDENT_A2_SLICE4_REVIEW / A1 STOP" if result.wasSuccessful() and len(MATRIX) == 15 else "REPAIR_REQUIRED",
                   "limitations": ["Offline/public synthetic numeric resources only; no live connector or HumanResponse override",
                                   "Process crash proof; no physical power-cut proof", "Exact served JS Node VM; no native browser rendering proof",
                                   "No independent A2 verdict; no merge or next-slice authorization"]}
        print(encode(receipt))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
