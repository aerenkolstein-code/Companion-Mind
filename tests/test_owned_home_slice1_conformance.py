"""A1 P2-S1 conformance; all behavioral observations use sanctioned TestPort.

Run: python -m unittest discover -s tests -p 'test_owned_home_slice1_*.py' -v
Receipt: python tests/test_owned_home_slice1_conformance.py --receipt
No internal Journal/FTS database queries, real secrets or external networking.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from http.client import HTTPConnection
import json
import os
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

from companion_mind.journal import Journal, JournalError
from companion_mind.owned_home.contracts import BASE_SHA, BASE_TREE, HomeError, VERSION, encode, fingerprint
from companion_mind.owned_home.shell import make_server
from companion_mind.owned_home.testport import execute

MATRIX = {}
SCOPE = {"universe_id": "synthetic-home", "access_subject_id": "synthetic-owner"}

# Execute the served, exact app.js in a fresh JS realm on every reload. DOM and
# storage are instrumented; fetch crosses the real loopback HTTP boundary. Node
# is already required by the repository CI; no browser/runtime package is added.
BROWSER_HARNESS = r"""
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', async () => {
  const config = JSON.parse(input), storage = new Map(Object.entries(config.storage || {}));
  const writes = [], calls = [], states = [];
  const key = 'owned-home-v1:' + config.origin;
  let elements, listeners, active, drop = null;
  const forbidden = () => { throw new Error('PERSISTENT_API_FORBIDDEN'); };
  async function settle() {
    do {
      await Promise.allSettled([...active]);
      await new Promise(resolve => setImmediate(resolve));
    } while (active.size);
  }
  async function load() {
    elements = Object.fromEntries(['form','message','submit','resume','next','status','reply']
      .map(id => [id, {value:'',textContent:'',disabled:false,hidden:['resume','next'].includes(id),
        handlers:{},addEventListener(event, fn) { this.handlers[event] = fn; }}]));
    listeners = {}; active = new Set();
    const document = {getElementById:id => elements[id]};
    Object.defineProperty(document, 'cookie', {get:forbidden,set:forbidden});
    const context = vm.createContext({document, location:{origin:config.origin}, crypto:webcrypto,
      localStorage:{getItem:k => storage.get(k) ?? null,
        setItem(k,v) { writes.push([String(k),String(v)]); storage.set(String(k),String(v)); },
        removeItem:k => storage.delete(k)},
      sessionStorage:{getItem:forbidden,setItem:forbidden,removeItem:forbidden},
      indexedDB:{open:forbidden}, caches:{open:forbidden},
      addEventListener:(event,fn) => listeners[event] = fn,
      fetch:(path,options) => {
        const mode = drop; drop = null;
        const record = {body:JSON.parse(options.body)}; calls.push(record);
        const task = (async () => {
          if (mode === 'before') throw new Error('SYNTHETIC_TRANSPORT_LOSS');
          const response = await fetch(config.origin + path, options);
          record.status = response.status; record.data = await response.json();
          if (mode === 'after') throw new Error('SYNTHETIC_TRANSPORT_LOSS');
          return {json:async () => record.data};
        })();
        active.add(task); task.then(() => active.delete(task), () => active.delete(task));
        return task;
      }});
    vm.runInContext(config.script, context);
    await settle();
  }
  await load();
  for (const step of config.steps) {
    if (step.event === 'reload') {
      if (listeners.pagehide) listeners.pagehide();
      await load();
    } else if (step.event !== 'inspect') {
      if ('text' in step) elements.message.value = step.text;
      drop = step.drop || null;
      const target = step.event === 'submit' ? 'form' : step.event;
      await elements[target].handlers[step.event === 'submit' ? 'submit' : 'click']({preventDefault(){}});
      await settle();
    }
    states.push({status:elements.status.textContent, reply:elements.reply.textContent,
      message:elements.message.value, resume_hidden:elements.resume.hidden,
      next_hidden:elements.next.hidden, storage:Object.fromEntries(storage), calls:calls.length});
  }
  process.stdout.write(JSON.stringify({writes,calls,states,key}));
});
"""


def request(number=1):
    return {"contract_version": VERSION, "scope": dict(SCOPE),
            "fixtures": [{**SCOPE, "source_id": "local-demo", "version": "v1",
                          "text": "Synthetic lexical evidence: local continuity."}],
            "grants": [{**SCOPE, "source_id": "local-demo", "version": "v1"}],
            "op": "turn", "turn": {**SCOPE, "contract_version": VERSION, "request_id": f"req-{number}",
                "session_id": "session-1", "turn_id": f"turn-{number}", "turn_no": number,
                "source_id": "local-demo", "source_version": "v1", "text": "local continuity",
                "observed_at": "2026-09-16T00:00:00+00:00", "budget_bytes": 4096}}


def operation(original, op, **fields):
    return {k: deepcopy(v) for k, v in original.items() if k in ("contract_version", "scope", "fixtures", "grants")} | {"op": op, **fields}


def process(directory, req, *, fault=None):
    command = [sys.executable, "-m", "companion_mind.owned_home.testport", "--store", str(directory)]
    if fault:
        command += ["--fault", fault]
    return subprocess.run(command, input=encode(req), capture_output=True, text=True, cwd=ROOT, timeout=15)


def stable_events(result):
    return [{k: e[k] for k in ("event_id", "actor_role", "status", "sequence_no", "request_id")} |
            {"fingerprint": e["receipt"]["fingerprint"]} for e in result["events"]]


class Slice1Conformance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "home"

    def call(self, req, *, store=None):
        return execute(store or self.store, deepcopy(req))

    def child(self, req, *, store=None):
        child = process(store or self.store, req)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        return json.loads(child.stdout)["result"]

    def passed(self, case, **evidence):
        MATRIX[case] = {"status": "PASS", **evidence}

    def test_ts1_01_durable_before_cognition(self):
        r = self.call(request())
        order = r["execution_order"]
        self.assertLess(order.index("USER_DURABLE_RECEIPT"), order.index("PERMISSION_ALLOW"))
        self.assertLess(order.index("CONTEXT_READY"), order.index("PROVIDER_STUB_INVOKED"))
        self.assertEqual(r["receipts"]["user"]["durability"], "SQLITE_WAL_FULL")
        self.assertLess(r["receipts"]["user"]["commit_generation"], r["receipts"]["assistant"]["commit_generation"])
        # Public dependency fault proves failure prevents dispatch, rather than
        # trusting trace labels alone. A019 remains responsible for fsync proof.
        with patch.object(Journal, "ingest", side_effect=JournalError("SYNTHETIC_IO_FAILURE")), \
             patch.object(Journal, "append_user_then_invoke_stub") as dispatch:
            with self.assertRaisesRegex(JournalError, "SYNTHETIC_IO_FAILURE"):
                self.call(request(), store=self.root / "io-fail")
            dispatch.assert_not_called()
        self.passed("TS1-01", io_failure_blocks_dispatch=True, durability="SQLITE_WAL_FULL")

    def test_ts1_02_terminal_before_display(self):
        req = request()
        child = process(self.store, req, fault="BEFORE_DISPLAY")
        self.assertEqual(child.returncode, 86)
        self.assertEqual(child.stdout, "")
        r = self.child(operation(req, "observe", request_id="req-1"))
        self.assertEqual(r["status"], "complete")
        self.assertEqual(r["terminal_count"], 1)
        self.assertEqual(r["receipts"]["assistant"]["durability"], "SQLITE_WAL_FULL")
        self.assertEqual(r["counters"]["cognition_stub_invocations"], 0)
        self.assertTrue(r["visible_reply"])
        self.passed("TS1-02", hard_exit=86, pre_crash_output_bytes=0, durable_terminal_count=1)

    def test_ts1_03_user_crash_requires_explicit_resume(self):
        req = request()
        child = process(self.store, req, fault="AFTER_USER_DURABLE")
        self.assertEqual(child.returncode, 86)
        self.assertEqual(child.stdout, "")
        pending = self.child(operation(req, "observe", request_id="req-1"))
        for r in (pending, self.child(req), self.child(req)):
            self.assertEqual((r["status"], r["external_outcome"]), ("AWAIT_EXPLICIT_RESUME", "NOT_SENT"))
            self.assertEqual(r["event_count"], 1)
            self.assertEqual(r["counters"]["cognition_stub_invocations"], 0)
            self.assertIsNone(r["visible_reply"])
        wrong_scope = operation(req, "resume", request_id="req-1")
        wrong_scope["scope"]["universe_id"] = "other-universe"
        self.assertEqual(json.loads(process(self.store, wrong_scope).stdout)["error"], "RESUME_TARGET_MISSING")
        resumed = self.child(operation(req, "resume", request_id="req-1"))
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(resumed["user_event_id"], pending["user_event_id"])
        self.assertEqual(resumed["terminal_count"], 1)
        # Both the original full-turn seam and the content-free handle seam
        # replay the same terminal without another invocation.
        for again in (self.child(req | {"resume": True}),
                      self.child(operation(req, "resume", request_id="req-1"))):
            self.assertEqual(again["assistant_event_id"], resumed["assistant_event_id"])
            self.assertEqual(again["counters"]["cognition_stub_invocations"], 0)
        self.passed("TS1-03", restarts=3, implicit_invocations=0, explicit_resume="PASS",
                    handle_resume_scope="ENFORCED", handle_resume_content_source="A019")

    def test_ts1_04_uniqueness_and_unknown_recovery(self):
        req = request()
        first = self.child(req)
        for _ in range(3):
            repeat = self.child(req)
            self.assertEqual(repeat["terminal_count"], 1)
            self.assertEqual(repeat["receipts"]["assistant"]["fingerprint"], first["receipts"]["assistant"]["fingerprint"])
        conflict = deepcopy(req)
        conflict["turn"]["text"] = "changed under same request"
        self.assertEqual(json.loads(process(self.store, conflict).stdout)["error"], "REQUEST_IDENTITY_CONFLICT")
        for point, status in (("AFTER_PROVIDER_INTENT", "failed"), ("AFTER_STUB_FRAME", "partial")):
            directory = self.root / point
            self.assertEqual(process(directory, req, fault=point).returncode, 86)
            recovered = self.child(operation(req, "observe", request_id="req-1"), store=directory)
            self.assertEqual(recovered["status"], status)
            self.assertEqual(recovered["external_outcome"], "UNKNOWN")
            self.assertEqual(recovered["terminal_count"], 1)
            self.assertEqual(recovered["counters"]["cognition_stub_invocations"], 0)
        events = self.call(operation(req, "safe_export"))
        self.assertEqual(len(events["events"]), 2)
        self.passed("TS1-04", duplicate_terminals=0, extra_ledgers=0, ambiguous_outcome="UNKNOWN")

    def test_ts1_05_authorized_scope_version_and_no_lexical_oracle(self):
        req = request()
        req["turn"]["text"] = "词面完全不同"
        r = self.call(req)
        self.assertEqual(r["projection"]["permission"]["decision"], "ALLOW")
        context = r["projection"]["context"]
        self.assertEqual(len(context["included"]), 1)
        self.assertEqual(context["included"][0]["version"], "v1")
        self.assertEqual(context["included"][0]["authority_class"], "SYNTHETIC_LOCAL")
        self.assertEqual(context["included"][0]["access_subject_id"], SCOPE["access_subject_id"])
        self.assertEqual(r["projection"]["retrieval"]["lexical_hits"], [])
        self.assertIn("Synthetic lexical evidence", r["visible_reply"])
        missing = request(2)
        missing["fixtures"] = []
        r = self.call(missing)
        self.assertEqual(r["projection"]["context"]["knowledge_state"], "UNKNOWN")
        empty = request(3)
        empty["fixtures"][0]["text"] = ""
        self.assertEqual(self.call(empty)["projection"]["context"]["knowledge_state"], "KNOWN_EMPTY")
        self.passed("TS1-05", exact_scope_and_version=True, authority_route_survives_lexical_miss=True,
                    empty_and_unknown_distinct=True)

    def test_ts1_06_permission_precedes_all_retrieval(self):
        for i, mutate in enumerate((lambda r: r.update(grants=[]),
                                    lambda r: r["grants"][0].update(decision="DENY"),
                                    lambda r: r["grants"][0].update(decision="UNKNOWN"),
                                    lambda r: r["grants"][0].update(universe_id="other-universe"),
                                    lambda r: r["grants"][0].update(access_subject_id="other-subject"),
                                    lambda r: r["grants"][0].update(version="v2"))):
            req = request(i + 1)
            req["fixtures"][0]["text"] = "DENIED-CONTENT-SENTINEL"
            mutate(req)
            r = self.call(req)
            self.assertEqual(r["projection"]["permission"]["decision"], "DENY")
            self.assertEqual(r["counters"]["candidate_retrievals"], 0)
            self.assertEqual(r["counters"]["authority_reads"], 0)
            self.assertEqual(r["counters"]["lexical_queries"], 0)
            self.assertNotIn("DENIED-CONTENT-SENTINEL", encode(r))
        original = request(7)
        self.call(original)
        revoked = operation(original, "observe", request_id="req-7")
        revoked["grants"] = []
        r = self.call(revoked)
        self.assertIsNone(r["visible_reply"])
        self.assertEqual(r["stop_reason"], "PERMISSION_REVOKED_REPLAY")
        self.passed("TS1-06", denial_variants=6, unauthorized_reads=0, content_leaks=0, revocation_replay="WITHHELD")

    def test_ts1_07_context_fingerprint_budget_and_secrets(self):
        req = request()
        a = self.call(req)["projection"]["context"]
        b = self.call(req, store=self.root / "fresh")["projection"]["context"]
        self.assertEqual(a, b)
        self.assertEqual(a["context_fingerprint"], fingerprint({k: v for k, v in a.items() if k != "context_fingerprint"}))
        tiny = request(2)
        tiny["turn"]["budget_bytes"] = 64
        r = self.call(tiny)
        c = r["projection"]["context"]
        self.assertEqual(c["included"], [])
        self.assertEqual(c["omitted"][0]["reason"], "CONTEXT_BUDGET_EXCEEDED")
        self.assertEqual(c["budget"]["included_bytes"], 0)
        self.assertEqual(c["budget"]["omitted_bytes"], c["budget"]["required"])
        self.assertEqual(c["status"], "BLOCKED")
        changed = request(3)
        changed["grants"][0]["version"] = changed["fixtures"][0]["version"] = changed["turn"]["source_version"] = "v2"
        self.assertNotEqual(self.call(changed)["projection"]["context"]["context_fingerprint"], a["context_fingerprint"])
        for key in ("text", "fixture"):
            unsafe = request(4)
            if key == "text":
                unsafe["turn"]["text"] = "api_key=SYNTHETIC-NON-CREDENTIAL-CANARY"
            else:
                unsafe["fixtures"][0]["text"] = "password=SYNTHETIC-NON-CREDENTIAL-CANARY"
            result = process(self.root / key, unsafe)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("SYNTHETIC-NON-CREDENTIAL-CANARY", result.stdout + result.stderr)
            self.assertFalse((self.root / key / "canonical" / "journal.sqlite3").exists())
        self.passed("TS1-07", context_fingerprint=a["context_fingerprint"], silent_truncations=0,
                    declared_secret_canary_leaks=0, budget_failure="EXPLICIT_OMISSION")

    def test_ts1_08_fts_independent_rebuildable_scope_preserving(self):
        req = request()
        self.call(req)
        before = stable_events(self.call(operation(req, "safe_export")))
        rebuild = operation(req, "rebuild", source_id="local-demo", version="v1")
        first = self.call(rebuild)
        self.assertTrue((self.store / "derived" / "lexical.sqlite3").exists())
        self.assertTrue((self.store / "canonical" / "journal.sqlite3").exists())
        self.assertFalse((self.store / "derived" / "lexical.sqlite3").samefile(self.store / "canonical" / "journal.sqlite3"))
        # Destroying a derived index is permitted; no database bytes are read.
        (self.store / "derived" / "lexical.sqlite3").unlink()
        second = self.call(rebuild)
        self.assertEqual(first["index"]["index_fingerprint"], second["index"]["index_fingerprint"])
        self.assertEqual(before, stable_events(self.call(operation(req, "safe_export"))))
        denied = deepcopy(rebuild)
        denied["scope"]["universe_id"] = "other-universe"
        self.assertEqual(self.call(denied)["status"], "DENY")
        self.passed("TS1-08", physical_separation=True, rebuild_fingerprint=first["index"]["index_fingerprint"], authority_writes=0)

    def test_ts1_09_wake_silent_all_checks(self):
        req = request()
        candidate = {**SCOPE, "event_id": "wake-1", "owner_subject_id": SCOPE["access_subject_id"],
                     "observed_at": "2026-09-16T00:00:00+00:00"}
        variations = [{}, {"salience": 0}, {"urgency": 0}, {"confidence": 0}, {"quiet_hours": True},
                      {"cooldown_remaining": 20}, {"repeat_count": 1}, {"owner_subject_id": "other"},
                      {"universe_id": "other"}]
        for change in variations:
            r = self.call(operation(req, "wake", candidate=candidate | change))
            self.assertEqual(r["action"], "SILENT")
            self.assertEqual((r["notifications"], r["continuations"], r["external_side_effects"]), (0, 0, 0))
            self.assertEqual(len(r["checks"]), 8)
            if change:
                self.assertTrue(r["suppressed_by"])
            self.assertEqual(r, self.call(operation(req, "wake", candidate=candidate | change)))
        self.passed("TS1-09", baseline_variants=len(variations), notifications=0, continuations=0, side_effects=0)

    def test_ts1_10_loopback_ui_reload_and_ingress_guards(self):
        for host in ("0.0.0.0", "::", "192.168.1.1", "localhost"):
            with self.assertRaisesRegex(HomeError, "LOOPBACK_ONLY"):
                make_server(self.store, host=host)
        server = make_server(self.store)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        port = server.server_port

        def http(method, path, body=None, headers=None):
            connection = HTTPConnection("127.0.0.1", port, timeout=4)
            default = {"Content-Type": "application/json", "X-Owned-Home": "1"}
            connection.request(method, path, body=encode(body) if body is not None else None, headers=headers or default)
            response = connection.getresponse()
            status, data = response.status, response.read().decode()
            connection.close()
            return status, data

        self.assertEqual(server.server_address[0], "127.0.0.1")
        status, html = http("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("/app.js", html)
        status, script = http("GET", "/app.js")
        self.assertIn("localStorage", script)
        self.assertIn("textContent", script)
        self.assertNotIn("innerHTML", script)

        def browser(steps, storage=None):
            child = subprocess.run(["node", "-e", BROWSER_HARNESS], cwd=ROOT,
                input=encode({"origin": f"http://127.0.0.1:{port}", "script": script,
                              "steps": steps, "storage": storage or {}}),
                capture_output=True, text=True, timeout=20)
            self.assertEqual(child.returncode, 0, child.stderr)
            return json.loads(child.stdout)

        def handles_only(result):
            self.assertTrue(result["writes"])
            for key, value in result["writes"]:
                self.assertEqual(key, result["key"])
                handle = json.loads(value)
                self.assertEqual(set(handle), {"request_id"})
                self.assertRegex(handle["request_id"], r"^[0-9a-f-]{36}$")

        canary = "SYNTHETIC-BROWSER-NON-CREDENTIAL-CANARY"
        for prefix in ("api_key=", "password=", "Bearer "):
            rejected = browser([{"event": "submit", "text": prefix + canary}])
            self.assertEqual(rejected["calls"][0]["data"], {"ok": False, "error": "UNSAFE_INPUT"})
            leaks = sum(canary in value for _, value in rejected["writes"])
            self.assertEqual(leaks, 0, "Credential-shaped input reached browser storage before rejection")
            self.assertFalse(self.store.exists(), "Rejected input must not open persistent runtime storage")
            self.assertEqual(rejected["states"][0]["message"], "")
            handles_only(rejected)

        plain = "PUBLIC-RAW-MESSAGE-MUST-NOT-ENTER-BROWSER-STORAGE"
        completed = browser([{"event": "submit", "text": plain}, {"event": "reload"}])
        handles_only(completed)
        self.assertNotIn(plain, encode(completed["writes"]))
        first_ui, reloaded_ui = [c["data"]["result"] for c in completed["calls"]]
        self.assertEqual(first_ui["assistant_event_id"], reloaded_ui["assistant_event_id"])
        self.assertEqual(reloaded_ui["counters"]["cognition_stub_invocations"], 0)
        self.assertEqual(completed["states"][1]["message"], "")

        lost = browser([{"event": "submit", "text": plain, "drop": "before"},
                        {"event": "reload"}, {"event": "submit", "text": plain}])
        handles_only(lost)
        self.assertEqual(lost["calls"][1]["data"]["result"]["status"], "NOT_FOUND")
        self.assertEqual(lost["states"][1]["message"], "")
        sent = [c["body"]["turn"] for c in lost["calls"] if c["body"]["op"] == "turn"]
        for field in ("request_id", "session_id", "turn_id"):
            self.assertEqual(sent[0][field], sent[1][field])
        self.assertEqual(lost["calls"][-1]["data"]["result"]["terminal_count"], 1)

        delivered = browser([{"event": "submit", "text": plain, "drop": "after"},
                             {"event": "reload"}])
        handles_only(delivered)
        self.assertEqual([c["body"]["op"] for c in delivered["calls"]], ["turn", "observe"])
        a, b = [c["data"]["result"] for c in delivered["calls"]]
        self.assertEqual(a["assistant_event_id"], b["assistant_event_id"])
        self.assertEqual(b["terminal_count"], 1)
        self.assertEqual(b["counters"]["cognition_stub_invocations"], 0)

        # Real subprocess exit after USER durable, then content-free reload and
        # explicit resume over HTTP. Only A019 can supply the original content.
        orphan = request()
        orphan_id = "11111111-1111-4111-8111-111111111111"
        orphan["turn"].update(request_id=orphan_id, session_id=orphan_id, turn_id=orphan_id, text=plain)
        self.assertEqual(process(self.store, orphan, fault="AFTER_USER_DURABLE").returncode, 86)
        before = self.call(operation(orphan, "observe", request_id=orphan_id))
        stored_handle = {completed["key"]: encode({"request_id": orphan_id})}
        resumed = browser([{"event": "inspect"}, {"event": "reload"},
                           {"event": "resume"}, {"event": "reload"}], stored_handle)
        handles_only(resumed)
        self.assertEqual([c["body"]["op"] for c in resumed["calls"]], ["observe", "observe", "resume", "observe"])
        for observed in resumed["calls"][:2]:
            r = observed["data"]["result"]
            self.assertEqual((r["status"], r["external_outcome"]), ("AWAIT_EXPLICIT_RESUME", "NOT_SENT"))
            self.assertEqual(r["counters"]["cognition_stub_invocations"], 0)
        self.assertNotIn(plain, encode([c["body"] for c in resumed["calls"]]))
        after = resumed["calls"][-1]["data"]["result"]
        self.assertEqual(after["receipts"]["user"]["fingerprint"], before["receipts"]["user"]["fingerprint"])
        self.assertEqual((after["status"], after["terminal_count"]), ("complete", 1))
        self.assertEqual(after["user_event_id"], before["user_event_id"])
        self.assertEqual(resumed["calls"][2]["data"]["result"]["counters"]["cognition_stub_invocations"], 1)
        self.assertEqual(after["counters"]["cognition_stub_invocations"], 0)

        # Upgrade cleanup discards every legacy field, retaining only a valid
        # generated identity. Neither old content nor forged IDs are re-written.
        legacy = dict(orphan["turn"], text="password=" + canary)
        upgraded = browser([{"event": "reload"}], {completed["key"]: encode(legacy)})
        handles_only(upgraded)
        self.assertNotIn(canary, encode(upgraded["writes"]) + encode(upgraded["states"]))
        forged = browser([{"event": "inspect"}],
                         {completed["key"]: encode({"request_id": "password=" + canary, "text": plain})})
        self.assertEqual(forged["writes"], [])
        self.assertEqual(forged["states"][0]["storage"], {})
        self.assertEqual(forged["calls"], [])

        # A public safe export has no secret/raw bodies, including rejected input.
        self.assertNotIn(canary, encode(self.call(operation(request(), "safe_export"))))
        body = {"contract_version": VERSION, "op": "turn", "turn": request()["turn"]}
        code, response = http("POST", "/v1/turn", body)
        self.assertEqual(code, 200, response)
        first = json.loads(response)["result"]
        self.assertEqual(http("GET", "/")[0], 200)
        second = json.loads(http("POST", "/v1/turn", body)[1])["result"]
        self.assertEqual(first["assistant_event_id"], second["assistant_event_id"])
        self.assertEqual(second["terminal_count"], 1)
        observe = {"contract_version": VERSION, "op": "observe", "request_id": "req-1"}
        self.assertEqual(json.loads(http("POST", "/v1/turn", observe)[1])["result"]["request_id"], "req-1")
        resumed_again = json.loads(http("POST", "/v1/turn", observe | {"op": "resume"})[1])["result"]
        self.assertEqual(resumed_again["assistant_event_id"], first["assistant_event_id"])
        self.assertEqual(resumed_again["counters"]["cognition_stub_invocations"], 0)
        self.assertEqual(http("POST", "/v1/turn", observe | {"op": "resume", "text": plain})[0], 400)
        self.assertEqual(json.loads(http("POST", "/v1/turn", observe | {"op": "resume", "request_id": "absent"})[1])["error"], "RESUME_TARGET_MISSING")
        self.assertEqual(http("GET", "/canonical/journal.sqlite3")[0], 404)
        self.assertEqual(http("POST", "/v1/turn", body | {"grants": []})[0], 400)
        self.assertEqual(http("POST", "/v1/turn", body, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(http("GET", "/", headers={"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(http("POST", "/v1/turn", body, {"Content-Type": "application/json", "X-Owned-Home": "1", "Origin": "https://attacker.invalid"})[0], 403)
        self.passed("TS1-10", loopback="127.0.0.1", http_reload_identity="STABLE", host_origin_and_route_guards="PASS",
                    browser_harness="EXACT_SERVED_JS_NODE_VM_REAL_LOOPBACK_HTTP", secret_input_variants=3,
                    browser_raw_content_writes=0, rejected_input_store_creations=0,
                    recovery_cases=["NOT_SENT_REENTRY", "USER_DURABLE_EXPLICIT_RESUME", "TERMINAL_REPLY_LOSS"],
                    legacy_content_cleanup="PASS", resume_content_source="A019_PUBLIC_EXPORT")

    def test_ts1_11_blackbox_and_no_external_network(self):
        req = request()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK_FORBIDDEN")), \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS_FORBIDDEN")):
            result = self.call(req)
            exported = self.call(operation(req, "safe_export"))
            self.assertEqual(result["provider_invocations"], 0)
            self.assertEqual(result["counters"]["external_side_effects"], 0)
        wire = encode(exported)
        for forbidden in (req["fixtures"][0]["text"], req["turn"]["text"], "journal.sqlite3", "canonical_event_log", "assistant_spool", "hidden_reasoning"):
            self.assertNotIn(forbidden, wire)
        for op in ("query_db", "provider", "drive", "notify", "write_authority"):
            child = process(self.store, operation(req, op))
            self.assertEqual(json.loads(child.stdout)["error"], "OPERATION_NOT_IN_SLICE")
        private = request()
        private["fixtures"][0]["public_safe"] = False
        self.assertEqual(json.loads(process(self.root / "private", private).stdout)["error"], "PUBLIC_SYNTHETIC_FIXTURE_REQUIRED")
        self.passed("TS1-11", network_guard="CONNECT_AND_DNS_DENIED", internal_db_oracles=0,
                    private_fixture_admission="DENY", safe_export_bodies=0)

    def test_ts1_12_repository_preflight_and_repeatability(self):
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(config["project"]["dependencies"], [])
        self.assertGreaterEqual(sys.version_info, (3, 11))
        with sqlite3.connect(":memory:") as probe:
            probe.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
        # WO-A1-A029-P2S5-01 section 4.3: repository preflight only.
        # Fresh base 40389094/tree3a0b5b51; all S4 tool semantics and TS4 protected.
        root_gitignore_base_blob = "6174fe7334a7d283b9096f9ce43a72f679af6030"
        root_gitignore_approved_blob = "e41bd00ee2232636bf669b900d8109260564f904"
        root_gitignore_approved_append = (b"/tests/.ca_cli_scratch/\n/ca-cli-*/\n/client_secret*.json\n"
                                          b"/connector-alpha-binding.json\n/connector-alpha-result.json\n")
        allowed = {"companion_mind/owned_home/" + name + ".py" for name in
                   ("runtime", "shell", "testport", "trace", "human_control", "continuation")}
        allowed.update({
            "tests/test_owned_home_slice1_conformance.py",
            "companion_mind/owned_home/source_pack.py",
            "companion_mind/owned_home/readonly_session.py",
            "tests/test_owned_home_p3s1_conformance.py",
            "docs/owned_home_p3s1_contract_v1.md",
            # Separate authorized P3-S2 offline read-guard prototype surface.
            "companion_mind/owned_home/docs_read_guard.py",
            "tests/test_owned_home_docs_read_guard.py",
            "docs/owned_home_docs_read_guard_v1.md",
            # CA-01 Connector Alpha is a separate, bounded Windows read path.
            # Each path is explicit so this gate cannot become a prefix exemption.
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
            "companion_mind/connector_s3/credentials.py",
            "tests/test_connector_s3_credentials.py",
            "tests/test_connector_s3_transport.py",
            "tests/test_connector_s3_provider.py",
            "tests/test_connector_s3_executor.py",
            "tests/test_connector_s3_docs_plan.py",
            "tests/test_connector_s3_recovery.py",
            "tests/test_connector_s3_native.py",
            "tests/test_connector_s3_policy.py",
            "docs/connector_s3_local_design.md",
        })
        def permitted(path):
            # Explicitly approved C1 S0 publication compatibility allowance.
            c1_prefixes = (
                "companion_mind/browser_sidecar/",
                "docs/browser_sidecar/",
                "tests/fixtures/c1_chatgpt_web_v0_1/",
            )
            return (
                path in allowed
                or path == "tests/test_owned_home_slice5_conformance.py"
                or path == "tests/test_browser_sidecar_s0.py"
                or path.startswith(c1_prefixes)
            )
        changed = set(git("diff", "--name-only", "HEAD").splitlines()) | set(git("ls-files", "--others", "--exclude-standard").splitlines())
        self.assertTrue(all(permitted(p) for p in changed), changed)
        self.assertFalse(permitted("companion_mind/connector_alpha/unapproved.py"))
        self.assertFalse(permitted("companion_mind/owned_home/action_control.py"))
        self.assertEqual(git("hash-object", ".gitignore"), root_gitignore_approved_blob)
        approved_ignore = (ROOT / ".gitignore").read_bytes()
        self.assertTrue(approved_ignore.endswith(root_gitignore_approved_append))
        base_ignore = approved_ignore[:-len(root_gitignore_approved_append)]
        base_blob = subprocess.check_output(["git", "hash-object", "-w", "--stdin"],
                                            cwd=ROOT, input=base_ignore).decode().strip()
        self.assertEqual(base_blob, root_gitignore_base_blob)
        # Shallow CI need not contain the parent commit object. Reconstruct the
        # unchanged protected tree by removing only the approved mutable surface from
        # HEAD using a temporary index. Exact Git tree equality proves *all*
        # remaining files unchanged, without fetching or weakening the gate.
        with tempfile.TemporaryDirectory() as index_directory:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(index_directory) / "index"))
            subprocess.run(["git", "read-tree", "HEAD"], cwd=ROOT, env=env, check=True)
            added_surface = [p for p in git("ls-files").splitlines() if permitted(p)]
            subprocess.run(["git", "update-index", "--force-remove", "--", *added_surface],
                           cwd=ROOT, env=env, check=True)
            subprocess.run(["git", "update-index", "--cacheinfo", "100644", base_blob, ".gitignore"],
                           cwd=ROOT, env=env, check=True)
            reconstructed = subprocess.check_output(["git", "write-tree"], cwd=ROOT, env=env, text=True).strip()
            self.assertEqual(reconstructed, "60b0dc12c68bddf6edb508345c6c6fbccf48cdf7")
        scan_names = {name for name in allowed
                      if name.startswith("companion_mind/owned_home/") and name.endswith(".py")}
        scan_names |= {"companion_mind/owned_home/contracts.py", "companion_mind/owned_home/index.py",
                       "companion_mind/owned_home/router.py", "companion_mind/owned_home/context.py",
                       "companion_mind/owned_home/model_gateway.py", "companion_mind/owned_home/tool_gateway.py",
                       "companion_mind/owned_home/permission.py", "companion_mind/owned_home/action_control.py"}
        for name in sorted(scan_names):
            source = (ROOT / name).read_text()
            tree = ast.parse(source)
            if not name.endswith(("shell.py", "index.py")):
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                        self.assertFalse(any(m.split(".")[0] in {"sqlite3", "socket", "http", "urllib", "requests"} for m in modules))
            self.assertNotIn("canonical_event_log", source)
            self.assertNotIn("turn_attempt_control", source)
            self.assertNotIn("journal._", source)
        runs = []
        for run in range(2):
            directory = self.root / f"fresh-{run}"
            for number in range(1, 21):
                r = self.call(request(number), store=directory)
                self.assertEqual(r["status"], "complete")
                self.assertEqual(r["terminal_count"], 1)
            events = stable_events(self.call(operation(request(), "safe_export"), store=directory))
            self.assertEqual(len(events), 40)
            self.assertEqual([e["sequence_no"] for e in events], list(range(1, 41)))
            runs.append(fingerprint(events))
        self.assertEqual(runs[0], runs[1])
        self.passed("TS1-12", repository="aerenkolstein-code/Companion-Mind", runtime_dependencies=[],
                    FTS5="PASS", fresh_runs=2, turns_per_run=20, events_per_run=40,
                    canonical_fingerprint=runs[0], loss=0, disorder=0, duplicate_terminals=0)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Slice1Conformance)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if "--receipt" in sys.argv:
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
        receipt = {"work_order": "WO-A1-A029-P2S1-01", "base_sha": BASE_SHA, "base_tree": BASE_TREE,
                   "head_sha": git("rev-parse", "HEAD"), "head_tree": git("rev-parse", "HEAD^{tree}"),
                   "working_tree_clean": not bool(git("status", "--porcelain")),
                   "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                   "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                   "matrix": MATRIX, "zero_tolerance_count": 0 if result.wasSuccessful() and len(MATRIX) == 12 else "NOT_EVALUABLE",
                   "scope": "OFFLINE / SYNTHETIC / PROCESS-CRASH / LOOPBACK ONLY",
                   "limitations": ["No physical power-cut test", "No live provider/Drive/authentication", "Declared secret patterns only; public-safe synthetic fixture admission required", "No independent A2 verdict"],
                   "status": "READY_FOR_INDEPENDENT_A2_SLICE1_REVIEW" if result.wasSuccessful() and len(MATRIX) == 12 else "NOT_READY / REPAIR_REQUIRED"}
        receipt["conformance_fingerprint"] = fingerprint(MATRIX)
        print(encode(receipt))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
