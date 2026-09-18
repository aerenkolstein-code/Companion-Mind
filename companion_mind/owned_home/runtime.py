"""P2-S1 orchestration over the unchanged public A019 Journal seam.

USER is ingested first; route/context prepares a deterministic StubScript, whose
execution is owned by A019's durable attempt state machine. Restart observes
canonical events via public export, never Journal tables. No secondary turn log.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import os
from pathlib import Path

from companion_mind.journal import Journal, StubScript
from .contracts import (VERSION, HomeError, Turn, encode, evaluate_wake, fingerprint,
                        identifier, permission)
from .index import LexicalIndex
from .context import ContextTurn, build_context, invalidation
from .router import route_authorities
from .trace import context_trace, model_trace, tool_trace, human_trace
from .model_gateway import (CapabilityRegistry, ModelTurn, ProfileRef, call_spec,
                            result_from_terminal, script_frames, spec_matches)
from .action_control import TOOL_FAULTS, ToolActionControl
from .tool_gateway import (ActionRequest, SkillRegistry, SyntheticToolAdapter, action_intent,
                           exact_binding, execution_receipt)
from .permission import POLICY, evaluate_permission
from .human_control import (HUMAN_FAULTS, IDENTITY, NOW, HumanControl, HumanResponse,
                            instant, resolve_owner)
from .continuation import budget_receipt, one_hop, resume_decision

FAULTS = {"AFTER_USER_DURABLE", "AFTER_PROVIDER_INTENT", "AFTER_STUB_FRAME", "BEFORE_DISPLAY"}
JOURNAL_FAULTS = {"AFTER_PROVIDER_INTENT": "F2", "AFTER_STUB_FRAME": "F3", "BEFORE_DISPLAY": "F5"}


class OwnedRuntime:
    def __init__(self, directory, *, scope, fixtures=(), grants=(), fault=None, model_profiles=None,
                 skill_contracts=None, tool_grants=(), tool_targets=(), human_owners=(), human_now=NOW):
        if fault is not None and fault not in FAULTS | TOOL_FAULTS | HUMAN_FAULTS:
            raise HomeError("UNKNOWN_FAULT")
        instant(human_now)
        self.human_owners, self.human_now = tuple(human_owners), human_now
        self._human_control = None
        self.scope, self.fixtures, self.grants = scope, tuple(fixtures), tuple(grants)
        self.models = CapabilityRegistry(model_profiles)
        self.skills = SkillRegistry(skill_contracts)
        self.tool_grants, self.tool_targets = tuple(tool_grants), tuple(tool_targets)
        if len({g.grant_id for g in self.tool_grants}) != len(self.tool_grants):
            raise HomeError("DUPLICATE_TOOL_GRANT")
        if len({(t.universe_id, t.access_subject_id, t.target_id) for t in self.tool_targets}) != len(self.tool_targets):
            raise HomeError("DUPLICATE_TOOL_TARGET")
        self._tool_control = self._tool_adapter = None
        keys = [(f.universe_id, f.access_subject_id, f.source_id, f.version,
                 getattr(f, "revision", "r1")) for f in self.fixtures]
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
        if self._human_control is not None:
            self._human_control.close()
        if self._tool_adapter is not None:
            self._tool_adapter.close()
        if self._tool_control is not None:
            self._tool_control.close()
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
        if users and "tool_request" in self._control(users[0]):
            return self._observe_tool(ActionRequest(**self._control(users[0])["tool_request"]))
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
        if "topic_id" in control["request"]:
            result["topic_id"] = control["request"]["topic_id"]
        if terminals:
            terminal = terminals[0]
            result["receipts"]["assistant"] = self.journal.ingest("A019", terminal)
            saved = terminal["metadata"]["extensions"]["owned_home"].get("projection", {})
            outcome = terminal["metadata"]["extensions"].get("a019_attempt", {})
            result.update(status=terminal["status"], external_outcome=outcome.get("external_outcome", "UNKNOWN"),
                          stop_reason=saved.get("stop_reason"), projection=saved,
                          visible_reply=terminal["content_payload"].get("text", ""))
            req = control["request"]
            if "topic_id" in req:
                result = self._observe_context(result, control, saved)
                if "model_intent" in req:
                    result = self._observe_model(result, saved, terminal)
                return result
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
        if "tool_request" in control:
            return self.tool_execute(ActionRequest(**control["tool_request"]), resume=True)
        turn_type = self._turn_type(control["request"])
        turn = turn_type(**control["request"], text=user["content_payload"]["text"])
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

    @staticmethod
    def _turn_type(request):
        return ModelTurn if "model_intent" in request else ContextTurn if "topic_id" in request else Turn

    def submit(self, turn, *, resume=False, expected_spec=None):
        if isinstance(turn, ContextTurn):
            return self._submit_context(turn, resume=resume, expected_spec=expected_spec)
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

    def _session_events(self, session_id):
        identifier(session_id)
        return sorted((e for e in self._events() if
                       e["metadata"]["extensions"]["owned_home"]["identity"]["session_id"] == session_id),
                      key=lambda e: e["sequence_no"])

    @staticmethod
    def _control(event):
        return event["metadata"]["extensions"]["owned_home"]

    def _topic_terminals(self, session_id, topic_id):
        return [e for e in self._session_events(session_id) if e["actor_role"] == "assistant" and
                self._control(e)["request"].get("topic_id") == topic_id and
                "working_set" in self._control(e).get("projection", {})]

    def _route_context(self, turn):
        return route_authorities(self.scope, turn.needs, self.fixtures, self.grants, self.counters)

    def _observe_context(self, result, control, saved):
        request = control["request"]
        turn = self._turn_type(request)(**request, text="")
        _, _, snapshot = self._route_context(turn)
        latest = self._topic_terminals(turn.session_id, turn.topic_id)
        premise = self._control(latest[-1])["request"]["premise_id"] if latest else turn.premise_id
        reasons = invalidation(saved["working_set"], snapshot, premise)
        result["topic_id"] = turn.topic_id
        result["working_set_id"] = saved["working_set"]["working_set_id"]
        result["next_turn_no"] = max(e["sequence_no"] for e in self._session_events(turn.session_id)) // 2 + 1
        if reasons:
            # No obsolete source refs, source bodies or recent tail can be
            # replayed under tightened permissions. Canonical evidence remains.
            result.update(visible_reply=None, stop_reason="DERIVED_STATE_INVALIDATED",
                          projection={"state": "INVALIDATED", "active": False,
                                      "topic_id": turn.topic_id, "invalidation_reasons": reasons,
                                      "rebuild_required": True,
                                      "context_fingerprint": saved["context"]["context_fingerprint"]})
        else:
            session_users = [e for e in self._session_events(turn.session_id) if e["actor_role"] == "user"]
            active = (result["status"] == "complete" and session_users and
                      self._control(session_users[-1])["identity"]["request_id"] == turn.request_id)
            result["projection"] = {**saved, "active_input": bool(active), "trace": {**saved["trace"],
                                      "terminal_reason": result["stop_reason"] or result["external_outcome"]}}
            result["projection"]["trace"].pop("trace_fingerprint", None)
            result["projection"]["trace"]["trace_fingerprint"] = fingerprint(result["projection"]["trace"])
        result["counters"] = dict(self.counters)
        result["presentation_order"] = ["USER_DURABLE_RECEIPT", "ASSISTANT_DURABLE_RECEIPT", "DISPLAY"]
        return result

    def session_state(self, session_id):
        events = self._session_events(session_id)
        topics = {}
        users = [e for e in events if e["actor_role"] == "user"]
        active_topic = self._control(users[-1])["request"].get("topic_id") if users else None
        for event in events:
            control = self._control(event)
            topic = control["request"].get("topic_id")
            if topic and event["actor_role"] == "user":
                topics[topic] = control["identity"]["request_id"]
        projections = []
        for topic, request_id in sorted(topics.items()):
            state = self.observe(request_id)
            projection = state.get("projection", {})
            projections.append({"topic_id": topic, "request_id": request_id,
                                "active": topic == active_topic and bool(projection.get("active_input")),
                                "working_set": projection.get("working_set"),
                                "boot_pack": projection.get("boot_pack"),
                                "state": projection.get("state", state["status"]),
                                "invalidation_reasons": projection.get("invalidation_reasons", []),
                                "stop_reason": state.get("stop_reason")})
        return {"contract_version": VERSION, "session_id": session_id, **self.scope.projection(),
                "active_topic": active_topic,
                "next_turn_no": max((e["sequence_no"] + 1) // 2 for e in events) + 1 if events else 1,
                "topics": projections, "authority": False, "derived_from": "A019_PUBLIC_EXPORT",
                "counters": dict(self.counters)}

    def _submit_context(self, turn, *, resume=False, preview=False, expected_spec=None):
        if turn.scope != self.scope:
            raise HomeError("SCOPE_DENIED")
        if type(resume) is not bool:
            raise HomeError("INVALID_RESUME")
        users, terminals = self._lookup(turn.request_id)
        previous_events = self._topic_terminals(turn.session_id, turn.topic_id)
        previous = self._control(previous_events[-1])["projection"]["working_set"] if previous_events else None
        if not turn.evidence_needs:
            original = self._control(users[0])["request"] if users else (
                self._control(previous_events[-1])["request"] if previous_events else None)
            if original is None:
                raise HomeError("INITIAL_TOPIC_NEEDS_REQUIRED")
            turn = replace(turn, evidence_needs=original["evidence_needs"])
        if users:
            if self._control(users[0])["request_fingerprint"] != fingerprint(turn.projection()):
                raise HomeError("REQUEST_IDENTITY_CONFLICT")
            if preview:
                raise HomeError("EXISTING_REQUEST_SPEC_LOCKED")
            if terminals or not resume:
                return self.observe(turn.request_id)
        elif resume:
            raise HomeError("RESUME_TARGET_MISSING")
        session_events = self._session_events(turn.session_id)
        prior_users = [e for e in session_events if e["actor_role"] == "user" and e["event_id"] != turn.identity["user_event_id"]]
        if not users:
            expected = max((e["sequence_no"] + 1) // 2 for e in session_events) + 1 if session_events else 1
            if turn.turn_no != expected:
                raise HomeError("SESSION_SEQUENCE_CONFLICT")
            completed = {self._control(e)["identity"]["request_id"] for e in session_events if e["actor_role"] == "assistant"}
            if any(self._control(e)["identity"]["request_id"] not in completed for e in prior_users):
                raise HomeError("PENDING_TURN_REQUIRES_RESUME")
        user = self._event(turn, "user")
        user_receipt = None if preview else self.journal.ingest("A019", user)
        if not preview and self.fault == "AFTER_USER_DURABLE":
            os._exit(86)
        model_turn = isinstance(turn, ModelTurn)
        selected, selection = self.models.select(turn.intent, turn.budget_bytes) if model_turn else (None, None)
        router, evidence, snapshot = self._route_context(turn)
        recent = []
        by_request = {self._control(e)["identity"]["request_id"]: e for e in prior_users}
        for event in previous_events:
            control = self._control(event)
            original = by_request.get(control["identity"]["request_id"])
            if original is not None:
                recent.append({"ref": {"user_event_id": original["event_id"], "assistant_event_id": event["event_id"],
                                       "turn_id": event["turn_id"]},
                               "user_text": original["content_payload"].get("text", ""),
                               "assistant_text": event["content_payload"].get("text", ""),
                               "valid": not invalidation(control["projection"]["working_set"], snapshot, turn.premise_id)})
        segments, last_topic = [], None
        for event in [*prior_users, user]:
            topic = self._control(event)["request"].get("topic_id")
            if topic != last_topic and topic == turn.topic_id:
                segments.append(event["event_id"])
            last_topic = topic
        last_active = self._control(prior_users[-1])["request"].get("topic_id") if prior_users else None
        context, working, boot, payload = build_context(
            turn, router, evidence, snapshot, previous=previous, recent=recent, segments=segments,
            reactivated=bool(previous and last_active != turn.topic_id), model_budget=selection)
        reason = context["stop_reason"]
        projection = {"context": context, "working_set": working, "boot_pack": boot,
                      "retrieval": router, "trace": context_trace(turn, router, context), "stop_reason": reason}
        if model_turn:
            return self._model_call(turn, user, user_receipt, projection, selected, selection,
                                    preview=preview, expected_spec=expected_spec)
        template = self._event(turn, "assistant", projection)
        reply = "Stopped: " + reason if reason else "Synthetic evidence: " + " | ".join(e["text"] for e in payload["evidence"])
        script = StubScript((reply,), "failed" if reason else "complete")
        receipt = self.journal.append_user_then_invoke_stub(user, template, attempt_id=turn.identity["attempt_id"], script=script)
        self.counters["cognition_stub_invocations"] += receipt["provider_invocations"]
        result = self.observe(turn.request_id)
        result["receipts"]["user"] = user_receipt
        result["receipts"]["assistant"] = receipt["assistant"]
        result["execution_order"] = ["USER_DURABLE_RECEIPT", *router["query_order"],
                                     "CONTEXT_" + context["status"], *receipt["trace"], "DISPLAY"]
        return result

    def model_registry(self, ref=None):
        if ref is None:
            return self.models.projection()
        profile = self.models.lookup(ProfileRef(**ref))
        return {"status": "RESOLVED" if profile else "EXACT_PROFILE_UNRESOLVED",
                "profile": profile.projection() if profile else None, "provider_invocations": 0}

    def model_preview(self, turn, supplied_spec=None):
        preview = self._submit_context(turn, preview=True)
        if supplied_spec is not None:
            valid = bool(preview["call_spec"] and spec_matches(supplied_spec, preview["call_spec"]))
            return {"valid": valid, "reason": "EXACT_SPEC_MATCH" if valid else "MODEL_SPEC_INVALID",
                    "provider_invocations": 0, "expected_spec_fingerprint":
                    preview["call_spec"]["spec_fingerprint"] if preview["call_spec"] else None}
        return preview

    def _model_call(self, turn, user, user_receipt, projection, selected, selection, *, preview, expected_spec):
        context = projection["context"]
        spec = call_spec(turn, selected, context) if selected and not projection["stop_reason"] else None
        if expected_spec is not None and not (spec and spec_matches(expected_spec, spec)):
            projection["stop_reason"] = "MODEL_SPEC_INVALID"
        reason = projection["stop_reason"]
        gateway = {"selection": selection, "call_spec": spec,
                   "context_fingerprint": context["context_fingerprint"],
                   "input_units": context["budget"]["included_bytes"],
                   "latency_ms": selected.latency_ms if selected else None, "stop_reason": reason}
        projection["model_gateway"] = gateway
        if preview:
            return {"preview_only": True, "identity": turn.identity, "selection": selection,
                    "context": context, "working_set": projection["working_set"],
                    "call_spec": spec, "stop_reason": reason, "provider_invocations": 0,
                    "model_trace": model_trace(gateway, None)}
        template = self._event(turn, "assistant", projection)
        if selected:
            template.update(provider=selected.provider_key, model=selected.model_key)
        if reason:
            # A pre-call STOP has no provider attempt. It still closes the
            # canonical turn and obeys terminal-before-display using public ingest.
            template["status"] = "failed"
            template["content_payload"] = {"text": "Stopped: " + reason}
            assistant = self.journal.ingest("A019", template)
            count, trace = 0, ["ASSISTANT_DURABLE"]
        else:
            frames, outcome = script_frames(spec)
            receipt = self.journal.append_user_then_invoke_stub(
                user, template, attempt_id=turn.identity["attempt_id"], script=StubScript(frames, outcome))
            assistant, count, trace = receipt["assistant"], receipt["provider_invocations"], receipt["trace"]
        self.counters["cognition_stub_invocations"] += count
        result = self.observe(turn.request_id)
        result["provider_invocations"] = count
        result["real_provider_invocations"] = 0
        result["receipts"].update(user=user_receipt, assistant=assistant)
        result["execution_order"] = ["USER_DURABLE_RECEIPT", "MODEL_PRE_CALL_SELECTION",
                                     *projection["retrieval"]["query_order"], "CONTEXT_" + context["status"],
                                     "MODEL_STOP" if reason else "MODEL_SPEC_VALIDATED", *trace, "DISPLAY"]
        return result

    def _observe_model(self, result, saved, terminal):
        gateway = saved["model_gateway"]
        spec = gateway["call_spec"]
        call_result = result_from_terminal(spec, terminal, gateway["input_units"], gateway["latency_ms"]) if spec else None
        trace = model_trace(gateway, call_result)
        result.update(model_result=call_result, model_trace=trace, real_provider_invocations=0)
        if call_result and call_result["outcome"] in ("UNKNOWN", "TIMEOUT"):
            result["stop_reason"] = "MODEL_" + call_result["outcome"] + "_REQUIRES_EXPLICIT_DECISION"
        # A019's complete/partial/failed status is evidence about stored text;
        # the distinct five-way ModelCallResult is never inferred from that status.
        if "trace" in result.get("projection", {}):
            result["projection"]["trace"]["model_gateway"] = trace
            result["projection"]["trace"].pop("trace_fingerprint", None)
            result["projection"]["trace"]["trace_fingerprint"] = fingerprint(result["projection"]["trace"])
        return result

    def wake(self, candidate):
        return evaluate_wake(candidate, self.scope)

    def _humans(self):
        if self._human_control is None:
            self._human_control = HumanControl(self.directory / "human-control")
        return self._human_control

    def _human_events(self, human_request_id=None):
        return [e for e in self.journal.export() if
                e["metadata"].get("extensions", {}).get("owned_human", {}).get("scope") == self.scope.projection()
                and (human_request_id is None or e["metadata"]["extensions"]["owned_human"]["identity"]["human_request_id"] == human_request_id)]

    def _human_evidence(self, record, kind, payload, at):
        req = record["request"]
        events = self._human_events(req["human_request_id"])
        prior = [e for e in events if e["metadata"]["extensions"]["owned_human"]["kind"] == kind]
        if prior:
            if len(prior) != 1 or prior[0]["content_payload"] != payload:
                raise HomeError("HUMAN_EVIDENCE_CONFLICT")
            return self.journal.ingest("A019", prior[0])
        key = fingerprint({"request": req["request_fingerprint"], "kind": kind})
        event_id = "oh-human-" + key
        control = {"contract_version": "human-evidence/1", "scope": self.scope.projection(),
                   "identity": {k: req[k] for k in IDENTITY}, "kind": kind,
                   "request_fingerprint": req["request_fingerprint"], "payload_fingerprint": fingerprint(payload)}
        event = {"event_id": event_id, "session_id": "oh-human-lane-" + req["request_fingerprint"],
                 "turn_id": req["turn_id"], "sequence_no": len(events) + 1,
                 "actor_role": "user" if kind == "RESPONSE" else "assistant" if kind == "TERMINAL" else "system-derived-visible-event",
                 "message_id": event_id, "persona_id": req["owner_id"], "relationship_id": None,
                 "provider": "offline-local", "model": "deterministic-slice5", "observed_at": at, "created_at": at,
                 "content_type": "application/json", "content_payload": payload, "status": "complete",
                 "source_ref": {"source_kind": "owned_client", "observation_type": "observed",
                                "source_id": req["human_request_id"], "uri": None, "adapter_version": "human-evidence/1"},
                 "attachment_ref": [], "correction_id": None, "correction_of": None, "redaction_state": "none",
                 "metadata": {"adapter": "A019", "adapter_version": "human-evidence/1", "ingest_id": event_id,
                              "knowledge": {}, "extensions": {"owned_human": control}}}
        return self.journal.ingest("A019", event)

    def human_preview(self, request):
        self.counters["continuations"] = 0
        if request.scope != self.scope:
            raise HomeError("SCOPE_DENIED")
        req = request.projection()
        owner = resolve_owner(req, self.human_owners)
        return {"status": "REQUEST_READY" if owner["unique"] else "HOLD", "owner": owner,
                "human_request": req if owner["unique"] else None, "continuation_count": 0,
                "notifications": 0, "authority": False}

    def human_request(self, request, *, candidate=None):
        preview = self.human_preview(request)
        if not preview["owner"]["unique"]:
            return preview | {"human_requests_created": 0}
        wake = None
        if candidate is not None:
            gate = evaluate_wake(candidate, self.scope)
            if not all(gate["checks"].values()):
                return {"status": "SILENT", "wake_gate": gate, "human_requests_created": 0,
                        "notifications": 0, "continuation_count": 0}
            wake = {"event_id": candidate.event_id, "candidate_fingerprint": fingerprint(asdict(candidate)),
                    "checks": gate["checks"], "decision": "REQUEST_HUMAN", "notifications": 0}
        req = preview["human_request"]
        if instant(self.human_now) < instant(req["created_at"]):
            raise HomeError("HUMAN_REQUEST_NOT_YET_VALID")
        record, created = self._humans().bind(req, preview["owner"], self.human_now, wake)
        self._tool_fault("HUMAN_AFTER_REQUEST_CONTROL")
        self._human_recover(record)
        return self._human_view(record) | {"human_requests_created": int(created)}

    def _human_record(self, human_request_id, request_fingerprint=None):
        record = self._humans().lookup(human_request_id, self.scope.projection())
        if not record:
            raise HomeError("HUMAN_REQUEST_NOT_FOUND")
        if request_fingerprint is not None and record["request"]["request_fingerprint"] != request_fingerprint:
            raise HomeError("INVALID_RESPONSE_BINDING")
        return record

    def _human_owner(self, record):
        owner = resolve_owner(record["request"], self.human_owners)
        if owner["owner_fingerprint"] != record["owner"]["owner_fingerprint"]:
            owner = {**owner, "unique": False, "status": "OWNER_BINDING_CHANGED"}
            owner.pop("owner_fingerprint")
            owner["owner_fingerprint"] = fingerprint(owner)
        return owner

    def _human_finish(self, record, reason):
        if record["state"] != "TERMINAL":
            record.update(state="TERMINAL", reason=reason, terminal_at=self.human_now)
            self._humans().save()
        if record["continuation"]:
            self._human_evidence(record, "CONTINUATION", record["continuation"], record["resume_at"])
        if record["reason"] in {"EXPIRED", "CANCELLED", "RECOVERY_UNKNOWN", "CLOCK_ROLLBACK"}:
            self._human_evidence(record, record["reason"], {"reason": record["reason"]}, record["terminal_at"])
        payload = {"reason": record["reason"], "continuation": record["continuation"],
                   "budget": budget_receipt(record["request"], record["reserved"]),
                   "response_fingerprint": record["response"]["response_fingerprint"] if record["response"] else None}
        self._human_evidence(record, "TERMINAL", payload, record["terminal_at"])
        self._tool_fault("HUMAN_AFTER_TERMINAL_DURABLE")

    def _human_recover(self, record):
        # Complete durable evidence, never dispatch from observation/reload.
        self._human_evidence(record, "REQUEST", {"request": record["request"], "owner": record["owner"], "wake": record["wake"]},
                             record["request"]["created_at"])
        if record["state"] == "CREATED":
            self._tool_fault("HUMAN_AFTER_REQUEST_DURABLE")
            record["state"] = "WAITING"
            self._humans().save()
        if record["state"] == "TERMINAL":
            self._human_finish(record, record["reason"])
            return
        if instant(self.human_now) < instant(record["last_observed_at"]):
            self._human_finish(record, "CLOCK_ROLLBACK")
            return
        record["last_observed_at"] = self.human_now
        # A019 RESPONSE was written before its derived control binding. Recover
        # exact raw evidence at this crash boundary; do not fabricate human input.
        if record["response"] is None:
            rows = [e for e in self._human_events(record["request"]["human_request_id"])
                    if e["metadata"]["extensions"]["owned_human"]["kind"] == "RESPONSE"]
            if rows:
                raw = rows[0]["content_payload"]
                response = HumanResponse(**raw["raw_response"])
                projection = response.projection()
                if (len(rows) != 1 or projection != raw["derived_response"] or
                        not self._human_exact(record, projection)):
                    raise HomeError("HUMAN_EVIDENCE_CONFLICT")
                record.update(response=projection, response_at=rows[0]["observed_at"], state="RESPONSE_DURABLE")
        self._humans().save()
        # Once dispatch intent exists, expiry cannot pretend an uncertain hop
        # never ran. A durable result may only be finalized, never recomputed.
        if record["reserved"]:
            self._human_finish(record, "ONE_HOP_COMPLETE" if record["continuation"] else "RECOVERY_UNKNOWN")
        elif instant(self.human_now) >= instant(record["request"]["expires_at"]):
            self._human_finish(record, "EXPIRED")
        elif record["response"] and record["response"]["normalized"]["command"] == "CANCEL":
            self._human_finish(record, "CANCELLED")

    @staticmethod
    def _human_exact(record, response):
        return (all(record["request"][k] == response[k] for k in IDENTITY) and
                record["request"]["request_fingerprint"] == response["request_fingerprint"])

    def human_respond(self, response):
        self.counters["continuations"] = 0
        record = self._human_record(response.human_request_id, response.request_fingerprint)
        projection = response.projection()
        if not self._human_exact(record, projection):
            raise HomeError("INVALID_RESPONSE_BINDING")
        self._human_recover(record)
        if not self._human_owner(record)["unique"]:
            raise HomeError("INVALID_RESPONSE_OWNER")
        if record["response"]:
            if record["response"] != projection:
                raise HomeError("HUMAN_RESPONSE_CONFLICT")
            return self._human_view(record) | {"response_replay": True}
        if record["state"] == "TERMINAL":
            raise HomeError("INVALID_RESPONSE_LIFECYCLE")
        self._human_evidence(record, "RESPONSE", {"raw_response": asdict(response), "derived_response": projection}, self.human_now)
        self._tool_fault("HUMAN_AFTER_RESPONSE_EVIDENCE")
        record.update(response=projection, response_at=self.human_now, state="RESPONSE_DURABLE")
        self._humans().save()
        self._tool_fault("HUMAN_AFTER_RESPONSE_DURABLE")
        if projection["normalized"]["command"] == "CANCEL":
            self._human_finish(record, "CANCELLED")
        return self._human_view(record) | {"response_replay": False}

    def human_observe(self, human_request_id):
        self.counters["continuations"] = 0
        record = self._humans().lookup(human_request_id, self.scope.projection())
        if not record:
            return {"status": "NOT_FOUND", "human_request_id": human_request_id, "continuation_count": 0}
        self._human_recover(record)
        return self._human_view(record)

    def human_resume(self, human_request_id, request_fingerprint, *, cancel=False):
        self.counters["continuations"] = 0
        record = self._human_record(human_request_id, request_fingerprint)
        self._human_recover(record)
        owner = self._human_owner(record)
        if not owner["unique"] or record["state"] == "TERMINAL":
            return self._human_view(record)
        if cancel:
            self._human_finish(record, "CANCELLED")
            return self._human_view(record)
        decision = resume_decision(record["request"], record["response"], owner, record["state"], record["reserved"])
        if decision["decision"] != "RESUME":
            return self._human_view(record)
        record.update(reserved=True, state="RESUME_INTENT_DURABLE", decision=decision, resume_at=self.human_now)
        self._humans().save()
        self._human_evidence(record, "RESUME_INTENT", {"decision": decision, "budget": budget_receipt(record["request"], True)}, self.human_now)
        self._tool_fault("HUMAN_AFTER_RESUME_INTENT")
        result = one_hop(record["request"], record["response"])
        self.counters["continuations"] += 1
        self._tool_fault("HUMAN_AFTER_CONTINUATION")
        record.update(continuation=result, state="CONTINUATION_COMPLETED")
        self._humans().save()
        self._tool_fault("HUMAN_AFTER_CONTINUATION_RECEIPT")
        self._human_finish(record, "ONE_HOP_COMPLETE")
        return self._human_view(record)

    def _human_view(self, record):
        owner = self._human_owner(record)
        reason = record["reason"]
        decision = resume_decision(record["request"], record["response"], owner, record["state"], record["reserved"], reason)
        events = self._human_events(record["request"]["human_request_id"])
        count = 1 if record["continuation"] else "UNKNOWN" if record["reserved"] else 0
        status = ("HOLD" if not owner["unique"] else "UNKNOWN" if count == "UNKNOWN" or reason == "CLOCK_ROLLBACK"
                  else "STOP" if record["state"] == "TERMINAL" else "RESPONSE_DURABLE" if record["response"] else "WAITING")
        value = {"contract_version": "human-control-projection/1", "status": status,
                 "human_request": record["request"] if owner["unique"] else None,
                 "human_response": record["response"] if owner["unique"] else None,
                 "state": record["state"], "owner": owner, "resume_decision": decision,
                 "executed_decision": record["decision"], "stop_reason": reason,
                 "budget": budget_receipt(record["request"], record["reserved"]),
                 "continuation": record["continuation"] if owner["unique"] else None,
                 "continuation_count": count, "continuation_upper_bound": int(record["reserved"]),
                 "continuations_this_call": self.counters["continuations"],
                 "terminal_count": sum(e["actor_role"] == "assistant" for e in events),
                 "evidence": [{"kind": e["metadata"]["extensions"]["owned_human"]["kind"],
                               "event_id": e["event_id"], "receipt": self.journal.ingest("A019", e)} for e in events],
                 "wake_gate": record["wake"], "authority": False, "canonical_evidence_owner": "A019",
                 "mutable_execution_control_only": True, "automatic_resumes": 0,
                 "notifications": 0, "authority_mutation_count": 0, "real_credential_reads": 0,
                 "real_external_side_effects": 0,
                 "presentation_order": ["EXACT_OWNER", "HUMAN_REQUEST_CONTROL_DURABLE", "A019_REQUEST_DURABLE", "DISPLAY"]}
        value["trace"] = human_trace(record, value)
        return value

    def _tools(self):
        if self._tool_control is None:
            self._tool_control = ToolActionControl(self.directory / "tool-control")
        return self._tool_control

    def _adapter(self):
        if self._tool_adapter is None:
            self._tool_adapter = SyntheticToolAdapter(self.directory / "synthetic-tools")
        return self._tool_adapter

    def _tool_fault(self, point):
        if self.fault == point:
            os._exit(86)

    def skill_registry(self, skill_id=None, skill_version=None):
        if skill_id is None:
            return self.skills.projection()
        skill = self.skills.lookup(skill_id, skill_version)
        return {"status": "RESOLVED" if skill else "EXACT_SKILL_UNRESOLVED",
                "skill": skill.projection() if skill else None, "tool_executions": 0}

    def _prepare_tool(self, action, *, state="UNSEEN", expected_intent=None, expected_decision=None):
        skill = self.skills.lookup(action.skill_id, action.skill_version)
        target = next((t for t in self.tool_targets if (t.target_id, t.universe_id, t.access_subject_id) ==
                       (action.target_id, action.universe_id, action.access_subject_id)), None)
        intent = action_intent(action, skill, target)
        context, decision = evaluate_permission(action, intent, skill, self.scope, self.tool_grants, control_state=state)
        invalid = ((expected_intent is not None and not exact_binding(expected_intent, intent)) or
                   (expected_decision is not None and not exact_binding(expected_decision, decision)))
        if invalid:
            context, decision = evaluate_permission(action, intent, skill, self.scope, self.tool_grants,
                                                    control_state=state, invalid_binding=True)
        return skill, target, intent, context, decision

    def action_preview(self, action, *, expected_intent=None, expected_decision=None):
        _, _, intent, context, decision = self._prepare_tool(action, expected_intent=expected_intent,
                                                            expected_decision=expected_decision)
        return {"contract_version": VERSION, "action_intent": intent, "permission_context": context,
                "permission_decision": decision, "policy": {**POLICY, "fingerprint": fingerprint(POLICY)},
                "status": decision["decision"], "tool_executions": 0, "provider_invocations": 0,
                "authority_mutation_count": 0, "preview_only": True}

    def _tool_event(self, action, actor, projection=None):
        event = self._event(action.turn(), actor, projection)
        control = self._control(event)
        control.update(identity=action.identity, tool_request=action.projection(),
                       tool_request_fingerprint=fingerprint(action.projection()))
        event.update(provider="offline-tool", model="synthetic-tools/v1")
        return event

    def tool_execute(self, action, *, resume=False, expected_intent=None, expected_decision=None):
        if action.scope != self.scope:
            raise HomeError("SCOPE_DENIED")
        if type(resume) is not bool:
            raise HomeError("INVALID_RESUME")
        skill, target, intent, context, decision = self._prepare_tool(
            action, expected_intent=expected_intent, expected_decision=expected_decision)
        control = self._tools()
        record = control.check(intent)
        users, terminals = self._lookup(action.request_id)
        if users:
            if self._control(users[0]).get("tool_request_fingerprint") != fingerprint(action.projection()):
                raise HomeError("REQUEST_IDENTITY_CONFLICT")
            if terminals or (record and record["state"] == "TERMINAL") or not resume:
                return self._observe_tool(action, expected_intent=expected_intent, expected_decision=expected_decision)
        elif resume:
            raise HomeError("RESUME_TARGET_MISSING")
        else:
            events = self._session_events(action.session_id)
            expected = max((e["sequence_no"] + 1) // 2 for e in events) + 1 if events else 1
            completed = {self._control(e)["identity"]["request_id"] for e in events if e["actor_role"] == "assistant"}
            if action.turn_no != expected:
                raise HomeError("SESSION_SEQUENCE_CONFLICT")
            if any(e["actor_role"] == "user" and self._control(e)["identity"]["request_id"] not in completed for e in events):
                raise HomeError("PENDING_TURN_REQUIRES_RESUME")
            self.journal.ingest("A019", self._tool_event(action, "user"))
            self._tool_fault("TOOL_AFTER_USER_DURABLE")
        if record is None:
            record = control.bind(intent, decision)
        if record["state"] == "PERMISSION_ALLOWED":
            # Re-check current grants on explicit pre-dispatch resume. A prior
            # decision never grants permission across revocation or input drift.
            control.reauthorize(record, decision)
            self._tool_fault("TOOL_AFTER_PERMISSION")
            if not decision["execution_allowed"]:
                control.advance(record, "TERMINAL", terminal_reason=decision["reason_code"])
            else:
                control.advance(record, "DISPATCH_INTENT_DURABLE")
                self._tool_fault("TOOL_AFTER_DISPATCH_INTENT")
                control.advance(record, "EXECUTING_OR_UNKNOWN")
                observation = self._adapter().execute(action, skill, target, decision)
                self._tool_fault("TOOL_AFTER_EFFECT")
                control.advance(record, "RECEIPT_OBSERVED", observation=observation,
                                receipt=execution_receipt(intent, decision, observation))
                self._tool_fault("TOOL_AFTER_RECEIPT")
                return self._finish_tool(action, record, explicit=True, executions=1)
        return self._finish_tool(action, record, explicit=resume, executions=0)

    def _finish_tool(self, action, record, *, explicit=False, executions=0):
        control = self._tools()
        intent, decision = record["intent"], record["permission"]
        if record["state"] in ("DISPATCH_INTENT_DURABLE", "EXECUTING_OR_UNKNOWN"):
            control.advance(record, "TERMINAL", receipt=execution_receipt(intent, decision),
                            terminal_reason="UNKNOWN_NO_REDISPATCH")
        if record["state"] == "RECEIPT_OBSERVED":
            _, _, current_intent, _, current_decision = self._prepare_tool(action)
            same = (current_intent == intent and current_decision == decision)
            if intent["permission_tier"] == "P2_REVERSIBLE_WRITE" and record["observation"]["reported_outcome"] == "SUCCESS":
                if not explicit and same:
                    return self._tool_result(action, record, executions=0, pending="EXPLICIT_READBACK_REQUIRED")
                readback = (self._adapter().readback(action, intent) if same else
                            {"status": "UNAVAILABLE", "observed_fingerprint": None})
                receipt = execution_receipt(intent, decision, record["observation"], readback)
                if readback["status"] == "VERIFIED":
                    control.advance(record, "READBACK_VERIFIED", receipt=receipt)
                    self._tool_fault("TOOL_AFTER_READBACK")
                else:
                    control.advance(record, "TERMINAL", receipt=receipt, terminal_reason="P2_READBACK_NOT_VERIFIED")
            else:
                control.advance(record, "TERMINAL", terminal_reason=None)
        if record["state"] == "READBACK_VERIFIED":
            control.advance(record, "TERMINAL", terminal_reason=None)
        if record["state"] == "TERMINAL":
            self._tool_fault("TOOL_AFTER_TERMINAL_CONTROL")
            self._publish_tool_evidence(action, record)
            self._tool_fault("TOOL_BEFORE_DISPLAY")
        return self._tool_result(action, record, executions=executions)

    def _publish_tool_evidence(self, action, record):
        receipt = record["receipt"]
        outcome = receipt["outcome"] if receipt else record["permission"]["decision"]
        evidence = {"action_id": action.action_id, "skill_ref": record["intent"]["skill_ref"],
                    "action_fingerprint": record["intent"]["intent_fingerprint"],
                    "permission_fingerprint": record["permission"]["decision_fingerprint"],
                    "receipt_fingerprint": receipt["receipt_fingerprint"] if receipt else None,
                    "outcome": outcome, "readback_status": receipt["readback"]["status"] if receipt else "NOT_DISPATCHED",
                    "connector_class": "synthetic-tools/v1", "authority_mutation_count": 0}
        projection = {"tool_evidence": evidence, "stop_reason": record["terminal_reason"]}
        event = self._tool_event(action, "assistant", projection)
        event["status"] = "complete" if outcome == "SUCCESS" else "partial" if outcome in ("PARTIAL", "UNKNOWN") else "failed"
        event["content_payload"] = {"text": "Synthetic tool: " + outcome}
        self.journal.ingest("A019", event)

    def _observe_tool(self, action, *, expected_intent=None, expected_decision=None):
        record = self._tools().lookup(action.action_id, self.scope.projection())
        if record is None:
            return {"contract_version": VERSION, **action.identity, "status": "AWAIT_EXPLICIT_RESUME",
                    "tool_executions": 0, "provider_invocations": 0, "visible_reply": None,
                    "terminal_count": 0, "event_count": 1, "stop_reason": "EXPLICIT_RESUME_REQUIRED"}
        if record["state"] not in ("PERMISSION_ALLOWED", "RECEIPT_OBSERVED"):
            self._finish_tool(action, record)
        return self._tool_result(action, record, executions=0, expected_intent=expected_intent,
                                 expected_decision=expected_decision)

    def tool_observe(self, action_id, *, resume=False):
        identifier(action_id)
        users = [e for e in self._events() if e["actor_role"] == "user" and
                 self._control(e).get("tool_request", {}).get("action_id") == action_id]
        if not users:
            return {"status": "NOT_FOUND", "action_id": action_id, "tool_executions": 0}
        action = ActionRequest(**self._control(users[0])["tool_request"])
        return self.tool_execute(action, resume=True) if resume else self._observe_tool(action)

    def _tool_result(self, action, record, *, executions=0, pending=None, expected_intent=None, expected_decision=None):
        _, _, current_intent, context, decision = self._prepare_tool(
            action, state=record["state"], expected_intent=expected_intent, expected_decision=expected_decision)
        if (record["permission"]["reason_code"] == "STALE_OR_INVALID_ACTION_PERMISSION_BINDING" and
                record["permission"]["authorization_binding_fingerprint"] == decision["authorization_binding_fingerprint"]):
            skill = self.skills.lookup(action.skill_id, action.skill_version)
            context, decision = evaluate_permission(action, current_intent, skill, self.scope, self.tool_grants,
                                                    control_state=record["state"], invalid_binding=True)
        changed = (current_intent != record["intent"] or decision != record["permission"])
        if changed and decision["execution_allowed"]:
            skill = self.skills.lookup(action.skill_id, action.skill_version)
            context, decision = evaluate_permission(action, current_intent, skill, self.scope, self.tool_grants,
                                                    control_state=record["state"], invalid_binding=True)
        receipt = record["receipt"] if not changed else None
        users, terminals = self._lookup(action.request_id)
        evidence = self._control(terminals[0])["projection"]["tool_evidence"] if terminals else None
        terminal = record["state"] == "TERMINAL"
        status = ("PERMISSION_CONTEXT_CHANGED" if changed else receipt["outcome"] if terminal and receipt else
                  decision["decision"] if terminal else "AWAIT_EXPLICIT_RECONCILE" if record["state"] == "RECEIPT_OBSERVED" else "AWAIT_EXPLICIT_RESUME")
        trace = tool_trace(record["intent"], decision, self._tools().projection(record), receipt)
        result = {"contract_version": VERSION, **action.identity, **self.scope.projection(), "status": status,
                  "action_intent": record["intent"], "permission_context": context, "permission_decision": decision,
                  "action_control": self._tools().projection(record), "tool_receipt": receipt, "tool_trace": trace,
                  "tool_executions": executions, "provider_invocations": 0, "automatic_redispatches": 0,
                  "authority_mutation_count": 0, "real_external_side_effects": 0, "real_credential_reads": 0, "spend": 0,
                  "event_count": len(users) + len(terminals), "terminal_count": len(terminals),
                  "next_turn_no": max((e["sequence_no"] + 1) // 2 for e in self._session_events(action.session_id)) + 1,
                  "canonical_evidence": evidence, "receipts": {"user": self.journal.ingest("A019", users[0])},
                  "visible_reply": terminals[0]["content_payload"]["text"] if terminals and not changed else None,
                  "stop_reason": "PERMISSION_CONTEXT_CHANGED" if changed else pending or record["terminal_reason"],
                  "presentation_order": ["USER_DURABLE", "TOOL_CONTROL_DURABLE", "A019_TERMINAL_DURABLE", "DISPLAY"] if terminals else ["USER_DURABLE", "TOOL_CONTROL_DURABLE"]}
        if terminals:
            result["receipts"]["assistant"] = self.journal.ingest("A019", terminals[0])
        if changed and record["receipt"]:
            result["historical_receipt_fingerprint"] = record["receipt"]["receipt_fingerprint"]
        if not changed and decision["execution_allowed"] and record["intent"]["permission_tier"] != "P0_PURE":
            result["synthetic_target"] = self._adapter().probe(record["intent"])
        return result

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
                 "receipt": self.journal.ingest("A019", e),
                 **({"tool_evidence": self._control(e)["projection"]["tool_evidence"]}
                    if self._control(e).get("projection", {}).get("tool_evidence") else {})} for e in self._events()] + [
            {"event_id": e["event_id"], "actor_role": e["actor_role"], "sequence_no": e["sequence_no"],
             "human_evidence": e["metadata"]["extensions"]["owned_human"],
             "receipt": self.journal.ingest("A019", e)} for e in self._human_events()]
