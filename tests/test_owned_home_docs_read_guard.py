"""WO-A1-A029-P3S2-DOCS-READ-GUARD-PROTOTYPE-01: offline RG-T01..20.

python tests/test_owned_home_docs_read_guard.py --receipt
All guard calls and crash children run behind a network-failing fixture.
"""
from __future__ import annotations

import ast
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import copy, deepcopy
from dataclasses import replace
import io
import json
import logging
import os
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion_mind.owned_home.contracts import HomeError, Scope, encode
from companion_mind.owned_home.docs_read_guard import (
    Binding, MIME, PROFILE, PROVIDER, OfflineDocsFixture, OpaqueHandle,
    ReadGuard, SyntheticBroker, WRITES,
)
from companion_mind.owned_home.permission import SyntheticGrant, evaluate_permission
from companion_mind.owned_home.tool_gateway import (
    ActionRequest, SkillContract, ToolTarget, action_intent,
)

BASE_SHA = "14f0e9cf00413a8ac3ad902b3b9c5641616970a1"
BASE_TREE = "529eab1585e2598a6da4c846a56e14c743f515c0"
PROTECTED_TREE = "2d210c5cd283d234ff438487513349430327ed4f"
ROOT_GITIGNORE_BASE_BLOB = "6174fe7334a7d283b9096f9ce43a72f679af6030"
ROOT_GITIGNORE_APPROVED_BLOB = "e41bd00ee2232636bf669b900d8109260564f904"
ROOT_GITIGNORE_APPROVED_APPEND = (b"/tests/.ca_cli_scratch/\n/ca-cli-*/\n/client_secret*.json\n"
                                  b"/connector-alpha-binding.json\n/connector-alpha-result.json\n")
CONNECTOR_ALPHA_FILES = frozenset({
    "companion_mind/connector_alpha/__init__.py",
    "companion_mind/connector_alpha/cli.py",
    "companion_mind/connector_alpha/contract.py",
    "companion_mind/connector_alpha/lifecycle.py",
    "companion_mind/connector_alpha/oauth.py",
    "companion_mind/connector_alpha/secret_store.py",
    "companion_mind/connector_alpha/session.py",
    "companion_mind/connector_alpha/transport.py",
    "tests/test_connector_alpha_cli.py",
    "tests/test_connector_alpha_desktop_client.py",
    "tests/test_connector_alpha_lifecycle.py",
    "tests/test_connector_alpha_loopback.py",
    "tests/test_connector_alpha_oauth_diagnostics.py",
    "tests/test_connector_alpha_session.py",
    "tests/test_connector_alpha_transport.py",
    "tests/test_connector_alpha_transport_diagnostics.py",
    "docs/connector_alpha_local_qualification.md",
    "docs/connector_alpha_binding_renewal.md",
    "docs/owned_home_connector_alpha_v1.md",
    "tests/.ca_cli_scratch/.gitignore",
    "tools/connector_alpha_native_probe.py",
    "companion_mind/connector_s3/__init__.py",
    "companion_mind/connector_s3/policy.py",
    "companion_mind/connector_s3/native.py",
    "companion_mind/connector_s3/stage.py",
    "companion_mind/connector_s3/broker.py",
    "companion_mind/connector_s3/oauth.py",
    "tests/test_connector_s3_stage.py",
    "tests/test_connector_s3_broker.py",
    "tests/test_connector_s3_oauth.py",
    "docs/connector_s3_delivery.md",
    "companion_mind/connector_s3/docs_plan.py",
    "companion_mind/connector_s3/recovery.py",
    "companion_mind/connector_s3/transport.py",
    "companion_mind/connector_s3/runner.py",
    "companion_mind/connector_s3/provider.py",
    "companion_mind/connector_s3/transactions.py",
    "companion_mind/connector_s3/executor.py",
    "tests/test_connector_s3_transport.py",
    "tests/test_connector_s3_provider.py",
    "tests/test_connector_s3_executor.py",
    "tests/test_connector_s3_docs_plan.py",
    "tests/test_connector_s3_recovery.py",
    "tests/test_connector_s3_native.py",
    "tests/test_connector_s3_policy.py",
    "docs/connector_s3_local_design.md",
})
ALLOWED_FILES = {
    "companion_mind/owned_home/docs_read_guard.py",
    "tests/test_owned_home_docs_read_guard.py",
    "docs/owned_home_docs_read_guard_v1.md",
    "tests/test_owned_home_p3s1_conformance.py",
    "tests/test_owned_home_slice1_conformance.py",
    "tests/test_browser_sidecar_s0.py",
} | CONNECTOR_ALPHA_FILES
CANARY = "SYNTHETIC_ONLY_RG_SECRET_CANARY_8b3e"
NOW = "2026-09-20T12:00:00+00:00"
MATRIX = {}
NETWORK_ATTEMPTS = 0
NETWORK_PATHS = ("socket.socket", "socket.create_connection", "socket.getaddrinfo",
                 "urllib.request.urlopen", "http.client.HTTPConnection.connect",
                 "http.client.HTTPSConnection.connect")


def fail_network(*args, **kwargs):
    global NETWORK_ATTEMPTS
    NETWORK_ATTEMPTS += 1
    raise AssertionError("NETWORK_CANARY_TRIGGERED")


def network_guard(stack):
    return [stack.enter_context(patch(name, side_effect=fail_network)) for name in NETWORK_PATHS]


def binding(generation=2):
    return Binding(PROVIDER, "synthetic-app", "synthetic-client", "synthetic-project",
                   "synthetic-subject", "synthetic-universe", "synthetic-doc", MIME,
                   "read-probe", "synthetic-credential-domain", generation, PROFILE,
                   "2026-09-20T00:00:00+00:00", "2026-09-21T00:00:00+00:00")


def resources():
    result = {}
    for rid in ("synthetic-doc", "same-name-doc", "provider-granted-neighbor"):
        result[rid] = {
            "display_name": "Same report name",
            "metadata": {"id": rid, "mime": MIME, "version": "v7",
                         "modified_at": "2026-09-20T11:00:00+00:00", "trashed": False},
            "body": {"id": rid, "mime": MIME, "version": "v7", "revision": "r7",
                     "coverage": "FULL", "text": "Public synthetic engineering plan."},
        }
    return result


def passed(case, **evidence):
    MATRIX[case] = {"status": "PASS", **evidence}


class ReadGuardConformance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.network = network_guard(self.stack)
        self.now = [NOW]
        self.broker = SyntheticBroker(CANARY)
        self.adapter = OfflineDocsFixture(resources())
        self.guard = self.make_guard("state", broker=self.broker, adapter=self.adapter)

    def make_guard(self, name, *, bound=None, broker=None, adapter=None):
        guard = ReadGuard(self.root / name, bound or binding(), broker or SyntheticBroker(CANARY),
                          adapter or OfflineDocsFixture(resources()), lambda: self.now[0])
        self.addCleanup(guard.close)
        return guard

    def tearDown(self):
        for network in self.network:
            self.assertEqual(network.call_count, 0, "Prototype attempted a network path")

    def denied_without_dispatch(self, request, reason=None):
        before = (self.broker.dereferences, len(self.adapter.calls))
        r = self.guard.execute(request)
        self.assertIn(r["status"], ("DENY", "BLOCKED"))
        if reason:
            self.assertEqual(r["reason"], reason)
        self.assertNotIn("text", r)
        self.assertEqual((self.broker.dereferences, len(self.adapter.calls)), before)
        return r

    def test_rg_t01_exact_authorized_read(self):
        r = self.guard.execute(self.guard.request())
        self.assertEqual((r["status"], r["resource_id"]), ("SUCCESS", "synthetic-doc"))
        self.assertEqual(self.broker.dereferences, 1)
        self.assertEqual(self.adapter.calls, [("metadata", "synthetic-doc"), ("body", "synthetic-doc"),
                                             ("metadata", "synthetic-doc")])
        self.assertEqual(r["binding_digest"], self.guard.binding.digest)
        self.assertIsInstance(r["resume_handle"], OpaqueHandle)
        passed("RG-T01", exact_binding=True, server_constructed_reads=3)

    def test_rg_t02_wrong_id(self):
        q = self.guard.request()
        q["binding"]["resource_id"] = "not-the-probe"
        self.denied_without_dispatch(q, "BINDING_MISMATCH")
        passed("RG-T02", credential_dereferences=0, adapter_probes=0)

    def test_rg_t03_same_name_different_id(self):
        q = self.guard.request()
        q["binding"]["resource_id"] = "same-name-doc"
        self.assertEqual(resources()["same-name-doc"]["display_name"], resources()["synthetic-doc"]["display_name"])
        self.denied_without_dispatch(q)
        passed("RG-T03", name_not_identity=True, adapter_probes=0)

    def test_rg_t04_provider_granted_neighbor(self):
        q = self.guard.request()
        q["binding"]["resource_id"] = "provider-granted-neighbor"
        self.assertIn(q["binding"]["resource_id"], resources())
        self.denied_without_dispatch(q)
        passed("RG-T04", provider_fixture_contains_neighbor=True, adapter_probes=0)

    def test_rg_t05_all_identity_dimensions(self):
        for field in ("provider", "app", "client", "project", "access_subject", "universe",
                      "purpose", "credential_domain", "mime", "policy_version"):
            for value in ("different", "UNKNOWN", None):
                with self.subTest(field=field, value=value):
                    q = self.guard.request()
                    q["binding"][field] = value
                    self.denied_without_dispatch(q)
        passed("RG-T05", binding_mutations=30, adapter_probes=0)

    def test_rg_t06_generation_and_expiry(self):
        for value in (1, 3, True, "2"):
            q = self.guard.request()
            q["binding"]["grant_generation"] = value
            self.denied_without_dispatch(q)
        self.now[0] = self.guard.binding.expires_at
        self.denied_without_dispatch(self.guard.request(), "EXPIRED")
        self.now[0] = "2026-09-19T23:59:59+00:00"
        self.denied_without_dispatch(self.guard.request(), "NOT_YET_VALID")
        passed("RG-T06", expiry_boundary_closed=True, unknown_generation_closed=True)

    def test_rg_t07_revoke_before_dispatch(self):
        stale = self.guard.request()
        self.guard.revoke(remote_result="failed")
        self.assertEqual(self.guard.epoch, 1)
        self.denied_without_dispatch(stale, "EPOCH_MISMATCH")
        self.denied_without_dispatch(self.guard.request(), "REVOKED")
        passed("RG-T07", remote_failure_cannot_reopen=True, credential_dereferences=0, adapter_probes=0)

    def test_rg_t08_revoke_inflight_and_before_delivery(self):
        for stop_after in (2, 3):
            adapter = OfflineDocsFixture(resources())
            guard = self.make_guard(f"race-{stop_after}", adapter=adapter)
            entered, release = threading.Event(), threading.Event()
            def block(_):
                if len(adapter.calls) == stop_after:
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("RACE_TIMEOUT")
            adapter.after_exchange = block
            results = []
            thread = threading.Thread(target=lambda: results.append(guard.execute(guard.request())))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                guard.revoke(remote_result="failed")
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0]["status"], "DISCARDED")
            self.assertNotIn("text", results[0])
            self.assertNotIn("resume_handle", results[0])
            self.assertEqual(len(adapter.calls), stop_after)
        passed("RG-T08", races=2, old_result_deliveries=0)

    def test_rg_t09_all_writes_before_credential_and_dispatch(self):
        required = {"edit", "append", "create", "copy", "move", "delete", "share", "permission", "batchUpdate"}
        self.assertTrue(required <= WRITES)
        for action in sorted(WRITES):
            self.denied_without_dispatch(self.guard.request(action=action), "WRITE_DENIED")
        passed("RG-T09", denied_actions=sorted(WRITES), credential_dereferences=0, adapter_probes=0)

    def test_rg_t10_unknown_action_and_endpoint_escape(self):
        for action in ("unknown", "list", "search", "children", "changes", "redirect", "GET", "READ", "grant_mutation"):
            self.denied_without_dispatch(self.guard.request(action=action), "ACTION_NOT_ALLOWED")
        for field in ("url", "method", "path", "endpoint", "transport", "fields", "on_result"):
            self.denied_without_dispatch(self.guard.request(**{field: "untrusted"}), "INVALID_REQUEST")
        self.denied_without_dispatch(self.guard.request(handle=OpaqueHandle()), "HANDLE_DENIED")
        passed("RG-T10", caller_endpoint_selection=False, forged_handle_denied=True)

    def test_rg_t11_generic_p2_allow_does_not_bleed(self):
        action = ActionRequest(action_id="action", task_id="task", request_id="request", session_id="session",
                               turn_id="turn", turn_no=1, universe_id="universe", access_subject_id="actor",
                               skill_id="synthetic.reversible_write", skill_version="v1", target_id="target",
                               idempotency_key="key", grant_id="grant", parameters={"value": 9})
        skill = SkillContract("synthetic.reversible_write")
        target = ToolTarget("target", "universe", "actor")
        intent = action_intent(action, skill, target)
        grant = SyntheticGrant("grant", "universe", "actor", ("target",), ("write",))
        _, decision = evaluate_permission(action, intent, skill, Scope("universe", "actor"), [grant])
        self.assertEqual(decision["decision"], "ALLOW")
        self.denied_without_dispatch(self.guard.request(action="synthetic.reversible_write"), "WRITE_DENIED")
        self.denied_without_dispatch(self.guard.request(permission_decision=decision), "INVALID_REQUEST")
        self.denied_without_dispatch(self.guard.request(declared_tier="P0_PURE"), "INVALID_REQUEST")
        passed("RG-T11", generic_p2_still_allow=True, p3s2_p2_denied=True)

    def test_rg_t12_secret_canary_all_egress(self):
        stdout, stderr, logs = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        sinks = []
        with redirect_stdout(stdout), redirect_stderr(stderr):
            for transport in (str, repr):
                self.assertNotIn(CANARY, transport(self.guard.handle))
                self.assertNotIn(CANARY, transport(self.broker))
            for exporter in (pickle.dumps, copy, deepcopy):
                with self.assertRaisesRegex(HomeError, "HANDLE_EXPORT_DENIED"):
                    exporter(self.guard.handle)
            sinks.append(self.guard.execute(self.guard.request(action=CANARY)))
            evil = resources()
            evil["synthetic-doc"]["body"]["text"] = CANARY
            guarded = self.make_guard("secret-response", adapter=OfflineDocsFixture(evil))
            blocked = guarded.execute(guarded.request())
            self.assertEqual(blocked["reason"], "SECRET_OUTPUT_BLOCKED")
            sinks.append(blocked)
            error = self.make_guard("secret-error", adapter=OfflineDocsFixture(resources(), error=CANARY))
            sinks.append(error.execute(error.request()))
            clean = self.guard.execute(self.guard.request())
            sinks.append(clean)
        # These are the output/receipt projections consumed by future contexts,
        # logs and journals, not a claim that a real model was called.
        projections = json.dumps({"model_context": sinks, "trace": sinks, "journal": sinks}, default=str)
        for output in (projections, stdout.getvalue(), stderr.getvalue(), logs.getvalue()):
            self.assertNotIn(CANARY, output)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(CANARY.encode(), path.read_bytes())
        passed("RG-T12", canary_exposures=0, handle_exports=0, egress_surfaces=7)

    def test_rg_t13_store_unavailable_locked_no_fallback(self):
        for state, reason in (("available", "SECRETSTORE_UNAVAILABLE"), ("locked", "SECRETSTORE_LOCKED")):
            broker = SyntheticBroker(CANARY, available=state != "available", locked=state == "locked")
            adapter = OfflineDocsFixture(resources())
            guard = self.make_guard(state, broker=broker, adapter=adapter)
            fake_file = mock_open(read_data=CANARY)
            with patch.dict(os.environ, {"GOOGLE_TOKEN": CANARY}), patch("builtins.open", fake_file):
                r = guard.execute(guard.request())
            self.assertEqual((r["status"], r["reason"]), ("BLOCKED", reason))
            self.assertEqual((broker.dereferences, len(adapter.calls), fake_file.call_count), (0, 0, 0))
            self.assertNotIn(CANARY, encode(r))
        cached = self.guard.execute(self.guard.request())["resume_handle"]
        self.broker.locked = True
        self.denied_without_dispatch(self.guard.request(resume_handle=cached, require_fresh=False), "SECRETSTORE_LOCKED")
        passed("RG-T13", fallback_reads=0, adapter_probes=0)

    def test_rg_t14_stable_metadata_body_metadata_asof(self):
        r = self.guard.execute(self.guard.request())
        self.assertEqual((r["status"], r["freshness"], r["coverage"]), ("SUCCESS", "AS_OF", "FULL"))
        self.assertEqual(r["observed_at"], NOW)
        self.assertEqual((r["version"], r["revision"]), ("v7", "r7"))
        self.assertNotIn("CURRENT", encode({k: v for k, v in r.items() if k != "resume_handle"}))
        passed("RG-T14", complete_asof_evidence=True, synthetic_snapshot_only=True)

    def test_rg_t15_mismatch_and_wrong_response(self):
        for variant in ("after", "body", "identity", "coverage", "future"):
            docs = resources()
            after = None
            expected = "STALE"
            if variant == "after":
                after = {**docs["synthetic-doc"]["metadata"], "version": "v8"}
            elif variant == "body":
                docs["synthetic-doc"]["body"]["version"] = "v8"
            elif variant == "identity":
                docs["synthetic-doc"]["body"]["id"] = "provider-granted-neighbor"
                expected = "DENY"
            elif variant == "coverage":
                docs["synthetic-doc"]["body"]["coverage"] = "NOT_LOADED"
                expected = "UNKNOWN"
            else:
                docs["synthetic-doc"]["metadata"]["modified_at"] = "2026-09-22T00:00:00+00:00"
                expected = "UNKNOWN"
            guard = self.make_guard(variant, adapter=OfflineDocsFixture(docs, metadata_after=after))
            r = guard.execute(guard.request())
            self.assertEqual(r["status"], expected)
            self.assertNotIn("text", r)
            self.assertNotIn("resume_handle", r)
        passed("RG-T15", incoherent_successes=0)

    def test_rg_t16_missing_reader_signals_unknown(self):
        for part, key in (("metadata", "version"), ("body", "version"), ("body", "revision")):
            docs = resources()
            docs["synthetic-doc"][part][key] = None
            adapter = OfflineDocsFixture(docs)
            guard = self.make_guard(part + key, adapter=adapter)
            bound = guard.binding
            r = guard.execute(guard.request())
            self.assertEqual(r["status"], "UNKNOWN")
            self.assertNotIn("text", r)
            self.assertEqual(guard.binding, bound)
            self.assertEqual(len(adapter.calls), 3)
        docs = resources()
        del docs["synthetic-doc"]["metadata"]["modified_at"]
        guard = self.make_guard("missing-field", adapter=OfflineDocsFixture(docs))
        self.assertEqual(guard.execute(guard.request())["reason"], "MISSING_OR_UNEXPECTED_FIELDS")
        passed("RG-T16", privilege_escalations=0, missing_signals_preserved=True)

    def test_rg_t17_safe_error_classes(self):
        expected = {"403": ("DENY", "ACCESS_DENIED_403"),
                    "404": ("UNKNOWN", "NOT_FOUND_OR_UNREADABLE_404"),
                    "timeout": ("UNKNOWN", "TIMEOUT_UNKNOWN")}
        for error, result in expected.items():
            adapter = OfflineDocsFixture(resources(), error=error)
            guard = self.make_guard(error, adapter=adapter)
            r = guard.execute(guard.request())
            self.assertEqual((r["status"], r["reason"]), result)
            self.assertNotIn("exists", r)
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(r["automatic_retry_count"], 0)
        passed("RG-T17", errors=expected, retries=0)

    def test_rg_t18_cache_never_fresh_and_revoke_invalidates(self):
        initial = self.guard.execute(self.guard.request())
        handle = initial["resume_handle"]
        r = self.guard.execute(self.guard.request(resume_handle=handle))
        self.assertEqual((r["status"], r["reason"]), ("UNKNOWN", "FRESH_READ_REQUIRED"))
        historical = self.guard.execute(self.guard.request(resume_handle=handle, require_fresh=False))
        self.assertEqual((historical["status"], historical["reason"]), ("AS_OF", "CACHED_AS_OF"))
        self.assertEqual(historical["observed_at"], initial["observed_at"])
        self.assertEqual((self.broker.dereferences, len(self.adapter.calls)), (1, 3))
        self.guard.revoke()
        self.denied_without_dispatch(self.guard.request(resume_handle=handle, require_fresh=False))
        self.guard.install_generation(binding(3))
        self.denied_without_dispatch(self.guard.request(resume_handle=handle, require_fresh=False), "RESUME_INVALID")
        passed("RG-T18", cached_fresh_successes=0, revoked_resume_deliveries=0)

    def test_rg_t19_authorities_and_network_unchanged(self):
        paths = [self.root / f"{name}.txt" for name in ("Current", "Agenda", "Persona", "Relationship", "Canon")]
        for path in paths:
            path.write_text("Synthetic business authority, immutable to this guard.")
        before = [p.read_bytes() for p in paths]
        self.guard.execute(self.guard.request())
        for action in WRITES:
            self.guard.execute(self.guard.request(action=action))
        self.assertEqual([p.read_bytes() for p in paths], before)
        self.assertEqual(self.adapter.business_writes, 0)
        source = (ROOT / "companion_mind/owned_home/docs_read_guard.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules = [n.name.split(".")[0] for n in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [(node.module or "").split(".")[0]]
            else:
                continue
            self.assertFalse(set(modules) & {"socket", "http", "urllib", "requests", "subprocess"})
        for network in self.network:
            network.assert_not_called()
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
        changed = set(git("diff", "--name-only", "HEAD").splitlines())
        changed |= set(git("ls-files", "--others", "--exclude-standard").splitlines())
        self.assertTrue(changed <= ALLOWED_FILES, changed)
        self.assertNotIn("companion_mind/connector_alpha/unapproved.py", ALLOWED_FILES)
        self.assertNotIn("companion_mind/owned_home/action_control.py", ALLOWED_FILES)
        self.assertEqual(git("hash-object", ".gitignore"), ROOT_GITIGNORE_APPROVED_BLOB)
        approved_ignore = (ROOT / ".gitignore").read_bytes()
        self.assertTrue(approved_ignore.endswith(ROOT_GITIGNORE_APPROVED_APPEND))
        base_ignore = approved_ignore[:-len(ROOT_GITIGNORE_APPROVED_APPEND)]
        base_blob = subprocess.check_output(["git", "hash-object", "-w", "--stdin"],
                                            cwd=ROOT, input=base_ignore).decode().strip()
        self.assertEqual(base_blob, ROOT_GITIGNORE_BASE_BLOB)
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / "index"))
            subprocess.run(["git", "read-tree", "HEAD"], cwd=ROOT, env=env, check=True)
            surface = [p for p in git("ls-files").splitlines() if p in ALLOWED_FILES]
            subprocess.run(["git", "update-index", "--force-remove", "--", *surface], cwd=ROOT, env=env, check=True)
            subprocess.run(["git", "update-index", "--cacheinfo", "100644", base_blob, ".gitignore"],
                           cwd=ROOT, env=env, check=True)
            tree = subprocess.check_output(["git", "write-tree"], cwd=ROOT, env=env, text=True).strip()
        self.assertEqual(tree, PROTECTED_TREE)
        passed("RG-T19", guarded_network_paths=len(NETWORK_PATHS), network_attempts=0,
               live_calls=0, real_credentials=0, business_writes=0, provider_spend=0,
               protected_tree=tree, allowed_files=sorted(ALLOWED_FILES))

    def test_rg_t20_crash_restart_lifecycle(self):
        # Child dies after fsync/replace, before the caller can receive a reply.
        self.guard.close()
        def crash(mode):
            p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--crash", mode,
                                str(self.root / "state")], capture_output=True, text=True, timeout=15)
            self.assertEqual(p.returncode, 91, p.stderr)
            self.assertNotIn(CANARY, p.stdout + p.stderr)
        crash("revoke")
        guard = self.make_guard("state")
        self.assertEqual(guard.epoch, 1)
        self.assertEqual(guard.execute(guard.request())["reason"], "REVOKED")
        guard.close()
        crash("generation")
        with self.assertRaisesRegex(HomeError, "LIFECYCLE_STATE_UNAVAILABLE"):
            self.make_guard("state", bound=binding(2))
        guard = self.make_guard("state", bound=binding(3))
        self.assertEqual(guard.epoch, 2)
        old = guard.request()
        old["binding"] = binding(2).projection()
        self.assertEqual(guard.execute(old)["status"], "DENY")
        result = guard.execute(guard.request())
        resume = result["resume_handle"]
        guard.close()
        reopened = self.make_guard("state", bound=binding(3))
        self.assertEqual(reopened.execute(reopened.request(resume_handle=resume, require_fresh=False))["reason"], "RESUME_INVALID")
        reopened.close()
        (self.root / "state" / "state.json").write_text('{"truncated":')
        with self.assertRaisesRegex(HomeError, "LIFECYCLE_STATE_UNAVAILABLE"):
            self.make_guard("state", bound=binding(3))
        (self.root / "state" / "state.json").unlink()
        with self.assertRaisesRegex(HomeError, "LIFECYCLE_STATE_MISSING"):
            self.make_guard("state", bound=binding(3))
        passed("RG-T20", durable_crash_points=2, generation_rollback_denied=True,
               lost_cache_not_restored=True, damaged_state_fail_closed=True)


def main():
    if "--crash" in sys.argv:
        mode, directory = sys.argv[-2:]
        with ExitStack() as stack:
            network_guard(stack)
            guard = ReadGuard(directory, binding(), SyntheticBroker(CANARY),
                              OfflineDocsFixture(resources()), lambda: NOW)
            if mode == "revoke":
                guard.revoke(after_durable=lambda: os._exit(91))
            elif mode == "generation":
                guard.install_generation(binding(3), after_durable=lambda: os._exit(91))
        return 92
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReadGuardConformance))
    if "--receipt" in sys.argv:
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
        passed_all = result.wasSuccessful() and len(MATRIX) == 20 and NETWORK_ATTEMPTS == 0
        print(encode({"work_order": "WO-A1-A029-P3S2-DOCS-READ-GUARD-PROTOTYPE-01 v0.1",
                      "profile": PROFILE, "base_sha": BASE_SHA, "base_tree": BASE_TREE,
                      "head_sha": git("rev-parse", "HEAD"), "head_tree": git("rev-parse", "HEAD^{tree}"),
                      "working_tree_clean": not bool(git("status", "--porcelain")),
                      "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                      "matrix": MATRIX, "network_guarded_paths": list(NETWORK_PATHS),
                      "application_network_attempts": NETWORK_ATTEMPTS,
                      "real_credentials": 0, "google_live_calls": 0, "business_writes": 0, "provider_spend": 0,
                      "zero_tolerance_count": 0 if passed_all else "NOT_EVALUABLE",
                      "limitations": ["OFFLINE_SYNTHETIC_ONLY", "REAL_PRINCIPAL_AND_GRANT_UNBOUND",
                                      "PROVIDER_ATOMICITY_AND_EXTERNAL_REVOKE_UNKNOWN",
                                      "OS_SECRETSTORE_NOT_QUALIFIED", "NO_MERGE_NO_LIVE"],
                      "status": "OFFLINE_RG_CONFORMANCE_PASS" if passed_all else "FAIL_RETURN_FOR_REPAIR"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
