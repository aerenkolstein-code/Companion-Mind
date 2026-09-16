"""P2-S1 orchestration over the unchanged public A019 Journal seam.

USER is ingested first; route/context prepares a deterministic StubScript, whose
execution is owned by A019's durable attempt state machine. Restart observes
canonical events via public export, never Journal tables. No secondary turn log.
"""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path

from companion_mind.journal import Journal, StubScript
from .contracts import (VERSION, HomeError, Turn, encode, evaluate_wake, fingerprint,
                        identifier, permission)
from .index import LexicalIndex

FAULTS = {"AFTER_USER_DURABLE", "AFTER_PROVIDER_INTENT", "AFTER_STUB_FRAME", "BEFORE_DISPLAY"}
JOURNAL_FAULTS = {"AFTER_PROVIDER_INTENT": "F2", "AFTER_STUB_FRAME": "F3", "BEFORE_DISPLAY": "F5"}


class OwnedRuntime:
    def __init__(self, directory, *, scope, fixtures=(), grants=(), fault=None):
        if fault is not None and fault not in FAULTS:
            raise HomeError("UNKNOWN_FAULT")
        self.scope, self.fixtures, self.grants = scope, tuple(fixtures), tuple(grants)
        keys = [(f.universe_id, f.access_subject_id, f.source_id, f.version) for f in self.fixtures]
        if len(set(keys)) != len(keys):
            raise HomeError("AUTHORITY_FIXTURE_CONFLICT")
        self.directory = Path(directory).absolute()
        created = not self.directory.exists()
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.is_symlink():
            raise HomeError("UNSAFE_HOME_PATH")
        if created:
            descriptor = os.open(self.directory.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self.fault = fault
        journal_path = self.directory / "canonical" / "journal.sqlite3"
        index_path = self.directory / "derived" / "lexical.sqlite3"
        if journal_path.exists() and index_path.exists() and os.path.samefile(journal_path, index_path):
            raise HomeError("INDEX_JOURNAL_ALIAS")
        self.journal = Journal(self.directory / "canonical", fault=JOURNAL_FAULTS.get(fault))
        try:
            self.index = LexicalIndex(self.directory / "derived")
        except BaseException:
            self.journal.close()
            raise
        self.counters = {"candidate_retrievals": 0, "authority_reads": 0, "lexical_queries": 0,
                         "provider_invocations": 0, "cognition_stub_invocations": 0,
                         "notifications": 0, "continuations": 0, "external_side_effects": 0,
                         "authority_writes": 0}

    def close(self):
        self.index.close()
        self.journal.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _events(self):
        # Public canonical export is only used for turn recovery/identity, never
        # as a retrieval oracle. Only this scoped synthetic slice is projected.
        return [e for e in self.journal.export() if
                e["metadata"].get("extensions", {}).get("owned_home", {}).get("scope")
                == self.scope.projection() and
                e["metadata"]["extensions"]["owned_home"].get("contract_version") == VERSION]

    def _event(self, turn, actor, projection=None):
        ident = turn.identity
        request = turn.projection()
        request.pop("text")
        control = {"contract_version": VERSION, "scope": turn.scope.projection(),
                   "identity": ident, "request": request,
                   "request_fingerprint": fingerprint(turn.projection())}
        if projection is not None:
            control["projection"] = projection
        event_id = ident["user_event_id" if actor == "user" else "assistant_event_id"]
        return {"event_id": event_id,
                # Namespacing prevents cross-universe/session sequence collisions.
                "session_id": "oh-session-" + fingerprint({**turn.scope.projection(), "session_id": turn.session_id}),
                "turn_id": turn.turn_id, "sequence_no": turn.turn_no * 2 - (actor == "user"),
                "actor_role": actor, "message_id": event_id, "persona_id": "synthetic-persona",
                "relationship_id": None, "provider": "offline-stub", "model": "deterministic-slice1",
                "observed_at": turn.observed_at, "created_at": turn.observed_at,
                "content_type": "text/plain", "content_payload": {"text": turn.text if actor == "user" else ""},
                "status": "complete", "source_ref": {"source_kind": "owned_client",
                    "observation_type": "observed", "source_id": turn.request_id, "uri": None,
                    "adapter_version": VERSION},
                "attachment_ref": [], "correction_id": None, "correction_of": None,
                "redaction_state": "none", "metadata": {"adapter": "A019", "adapter_version": VERSION,
                    "ingest_id": event_id, "knowledge": {}, "extensions": {"owned_home": control}}}

    def _lookup(self, request_id):
        identifier(request_id)
        rows = [e for e in self._events() if
                e["metadata"]["extensions"]["owned_home"]["identity"]["request_id"] == request_id]
        users = [e for e in rows if e["actor_role"] == "user"]
        terminals = [e for e in rows if e["actor_role"] == "assistant"]
        if len(users) > 1 or len(terminals) > 1:
            raise HomeError("DUPLICATE_CANONICAL_IDENTITY")
        return users, terminals

    def observe(self, request_id):
        users, terminals = self._lookup(request_id)
        if not users:
            return {"contract_version": VERSION, "request_id": request_id, "status": "NOT_FOUND",
                    "knowledge_state": "UNKNOWN", "provider_invocations": 0}
        user = users[0]
        control = user["metadata"]["extensions"]["owned_home"]
        result = {"contract_version": VERSION, **control["identity"], **self.scope.projection(),
                  "status": "AWAIT_EXPLICIT_RESUME", "external_outcome": "NOT_SENT",
                  "visible_reply": None, "stop_reason": "EXPLICIT_RESUME_REQUIRED",
                  "receipts": {"user": self.journal.ingest("A019", user)},
                  "event_count": len(users) + len(terminals), "terminal_count": len(terminals),
                  "provider_invocations": 0, "counters": dict(self.counters)}
        if terminals:
            terminal = terminals[0]
            result["receipts"]["assistant"] = self.journal.ingest("A019", terminal)
            saved = terminal["metadata"]["extensions"]["owned_home"].get("projection", {})
            outcome = terminal["metadata"]["extensions"].get("a019_attempt", {})
            result.update(status=terminal["status"], external_outcome=outcome.get("external_outcome", "UNKNOWN"),
                          stop_reason=saved.get("stop_reason"), projection=saved,
                          visible_reply=terminal["content_payload"].get("text", ""))
            req = control["request"]
            current = permission(self.scope, req["source_id"], req["source_version"], self.grants)
            if saved.get("permission", {}).get("decision") == "ALLOW" and current["decision"] != "ALLOW":
                result.update(visible_reply=None, stop_reason="PERMISSION_REVOKED_REPLAY",
                              projection={"context_fingerprint": saved.get("context", {}).get("context_fingerprint"),
                                          "state": "INVALIDATED_BY_PERMISSION", "permission": current})
            result["presentation_order"] = ["USER_DURABLE_RECEIPT", "ASSISTANT_DURABLE_RECEIPT", "DISPLAY"]
        return result

    def _source(self, turn, decision):
        if decision["decision"] != "ALLOW":
            return None
        self.counters["candidate_retrievals"] += 1
        matches = [f for f in self.fixtures if (f.universe_id, f.access_subject_id, f.source_id, f.version)
                   == (turn.universe_id, turn.access_subject_id, turn.source_id, turn.source_version)]
        if matches:
            self.counters["authority_reads"] += 1
            return matches[0]
        return None

    def resume(self, request_id):
        """Explicit recovery by stable handle; content comes only from A019.

        Never require the browser to retain/replay a raw turn. The existing
        submit path verifies the reconstructed request fingerprint and preserves
        terminal idempotency and the current permission gate.
        """
        users, _ = self._lookup(request_id)
        if not users:
            raise HomeError("RESUME_TARGET_MISSING")
        user = users[0]
        control = user["metadata"]["extensions"]["owned_home"]
        turn = Turn(**control["request"], text=user["content_payload"]["text"])
        return self.submit(turn, resume=True)

    def _context(self, turn, decision, source):
        ref = source.ref() if source else {"source_id": turn.source_id, "version": turn.source_version}
        evidence = [{"ref": ref, "text": source.text}] if source else []
        payload = {"user_text": turn.text, "evidence": evidence}
        required = len(encode(payload).encode("utf-8"))
        reason = ("PERMISSION_DENIED" if decision["decision"] != "ALLOW" else
                  "AUTHORITY_SOURCE_UNAVAILABLE" if source is None else
                  "CONTEXT_BUDGET_EXCEEDED" if required > turn.budget_bytes else None)
        included = evidence if reason is None else []
        # Ledger accounts for the exact serialized cognition input, not the
        # separately bounded public trace/envelope. No fragment truncation.
        actual_payload = payload if reason is None else None
        result = {"contract_version": VERSION, **turn.scope.projection(), "task_id": turn.identity["task_id"],
                  "model_profile": "deterministic-slice1/v1", "authority": False,
                  "resident_refs": [], "current_agenda_persona_relationship_refs": [],
                  "dynamic_boot_refs": [], "recent_exact_turn_refs": [turn.identity["user_event_id"]],
                  "capability_refs": ["context.build:P0_PURE", "authority.read:P1_SCOPED_READ"],
                  "included": [e["ref"] for e in included],
                  "omitted": [] if reason is None else [{"ref": ref, "reason": reason}],
                  "knowledge_state": ("NOT_LOOKED_UP" if decision["decision"] != "ALLOW" else
                                      "UNKNOWN" if source is None else
                                      "KNOWN_EMPTY" if source.text == "" else "KNOWN_VALUE"),
                  "conflicts": [], "unused_layers_state": "N_A",
                  "budget": {"unit": "UTF8_COGNITION_INPUT_BYTES", "limit": turn.budget_bytes,
                             "required": required, "included_bytes": required if reason is None else 0,
                             "omitted_bytes": required if reason is not None else 0,
                             "silent_truncations": 0},
                  "input_fingerprint": fingerprint(actual_payload),
                  "policy_fingerprint": decision["policy_fingerprint"], "status": "READY" if reason is None else "BLOCKED"}
        result["context_fingerprint"] = fingerprint(result)
        return result, reason, actual_payload

    def submit(self, turn, *, resume=False):
        if turn.scope != self.scope:
            raise HomeError("SCOPE_DENIED")
        if type(resume) is not bool:
            raise HomeError("INVALID_RESUME")
        users, terminals = self._lookup(turn.request_id)
        if users:
            saved = users[0]["metadata"]["extensions"]["owned_home"]
            if saved["request_fingerprint"] != fingerprint(turn.projection()):
                raise HomeError("REQUEST_IDENTITY_CONFLICT")
            if terminals or not resume:
                return self.observe(turn.request_id)
        elif resume:
            raise HomeError("RESUME_TARGET_MISSING")
        user = self._event(turn, "user")
        user_receipt = self.journal.ingest("A019", user)
        if self.fault == "AFTER_USER_DURABLE":
            os._exit(86)
        decision = permission(self.scope, turn.source_id, turn.source_version, self.grants)
        source = self._source(turn, decision)
        derived, hits, lexical_stop = None, [], None
        if decision["decision"] == "ALLOW":
            derived = self.index.rebuild([source] if source else [], decision)
            self.counters["lexical_queries"] += 1
            # Lexical recall is supporting evidence; the deterministic Authority
            # route remains primary even when literal terms do not match.
            try:
                hits = self.index.search(turn.text, decision)
            except HomeError as exc:
                if str(exc) != "LEXICAL_QUERY_BUDGET_EXCEEDED":
                    raise
                lexical_stop = str(exc)  # Explicit omission; Authority route still applies.
        context, reason, cognition_input = self._context(turn, decision, source)
        projection = {"permission": decision, "context": context, "index": derived,
                      "retrieval": {"route": "DETERMINISTIC_AUTHORITY", "scope": self.scope.projection(),
                                    "authority_class": "SYNTHETIC_LOCAL", "source_version": turn.source_version,
                                    "coverage": context["knowledge_state"], "lexical_hits": hits,
                                    "lexical_status": "NOT_LOOKED_UP" if decision["decision"] != "ALLOW" else
                                                      "OMITTED" if lexical_stop else "QUERIED",
                                    "lexical_stop_reason": lexical_stop,
                                    "source_refs": [source.ref()] if source else []}, "stop_reason": reason}
        template = self._event(turn, "assistant", projection)
        script = StubScript(("Stopped: " + reason if reason else
                             "Synthetic evidence: " + cognition_input["evidence"][0]["text"],),
                            "failed" if reason else "complete")
        receipt = self.journal.append_user_then_invoke_stub(
            user, template, attempt_id=turn.identity["attempt_id"], script=script)
        self.counters["cognition_stub_invocations"] += receipt["provider_invocations"]
        result = self.observe(turn.request_id)
        result["receipts"]["user"] = user_receipt
        result["receipts"]["assistant"] = receipt["assistant"]
        result["execution_order"] = ["USER_DURABLE_RECEIPT", "PERMISSION_" + decision["decision"],
                                     "CONTEXT_" + context["status"], *receipt["trace"], "DISPLAY"]
        return result

    def wake(self, candidate):
        return evaluate_wake(candidate, self.scope)

    def rebuild(self, source_id, version):
        decision = permission(self.scope, source_id, version, self.grants)
        if decision["decision"] != "ALLOW":
            return {"permission": decision, "status": "DENY", "candidate_retrievals": 0}
        source = [f for f in self.fixtures if (f.universe_id, f.access_subject_id, f.source_id, f.version) ==
                  (self.scope.universe_id, self.scope.access_subject_id, source_id, version)]
        return {"permission": decision, "index": self.index.rebuild(source, decision)}

    def safe_export(self):
        # No bodies, raw metadata, filesystem locators or internal DB contents.
        return [{"event_id": e["event_id"], "actor_role": e["actor_role"], "status": e["status"],
                 "sequence_no": e["sequence_no"], "request_id": e["metadata"]["extensions"]["owned_home"]["identity"]["request_id"],
                 "receipt": self.journal.ingest("A019", e)} for e in self._events()]
