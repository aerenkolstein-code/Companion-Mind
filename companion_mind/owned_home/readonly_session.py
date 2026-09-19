"""P3-S1 read-only continuity profile over the public A019 Journal seam."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from companion_mind.journal import StubScript
from .contracts import (VERSION, HomeError, Scope, encode, exact_keys, fingerprint,
                        identifier, safe_text)
from .source_pack import (ANSWER_VERSION, READONLY_FAULTS, READONLY_OPS,
                          READONLY_PROFILE, RESUME_VERSION, activate_bundle,
                          load_active, read_manifest, validate_bundle,
                          validate_grant)
from .trace import readonly_trace

CONTEXT_VERSION = "readonly-context/1"

TURN_FIELDS = (
    "task_id", "session_id", "request_id", "turn_id", "turn_no",
    "package_id", "package_version", "manifest_digest", "question",
    "universe_id", "access_subject_id", "evidence_needs", "budget_bytes",
)
NEED_FIELDS = ("source_id", "selector")


def _now(value):
    if not isinstance(value, str):
        raise HomeError("READONLY_CLOCK_REQUIRED")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HomeError("READONLY_CLOCK_REQUIRED") from None
    if result.tzinfo is None:
        raise HomeError("READONLY_CLOCK_REQUIRED")
    return result.astimezone(timezone.utc)


def _turn(value):
    exact_keys(value, TURN_FIELDS)
    for field in ("task_id", "session_id", "request_id", "turn_id",
                  "package_id", "package_version", "universe_id", "access_subject_id"):
        identifier(value[field])
    if not isinstance(value["manifest_digest"], str) or len(value["manifest_digest"]) != 64:
        raise HomeError("INVALID_MANIFEST_DIGEST")
    safe_text(value["question"], maximum=32768)
    if type(value["turn_no"]) is not int or not 1 <= value["turn_no"] <= 1_000_000:
        raise HomeError("INVALID_TURN_NUMBER")
    if type(value["budget_bytes"]) is not int or not 64 <= value["budget_bytes"] <= 65536:
        raise HomeError("INVALID_BUDGET")
    if not isinstance(value["evidence_needs"], list) or len(value["evidence_needs"]) > 5:
        raise HomeError("EVIDENCE_NEED_LIMIT")
    for need in value["evidence_needs"]:
        exact_keys(need, NEED_FIELDS)
        identifier(need["source_id"])
        safe_text(need["selector"], maximum=512)
    return value


def validate_readonly_operation(operation):
    if not isinstance(operation, dict):
        raise HomeError("INVALID_SHAPE")
    op = operation.get("op")
    if op in {"ro_info", "ro_package_validate", "ro_package_ingest", "ro_safe_export"}:
        exact_keys(operation, ("op",))
    elif op == "ro_turn":
        exact_keys(operation, ("op", "turn"))
        _turn(operation["turn"])
    elif op in {"ro_observe", "ro_resume"}:
        exact_keys(operation, ("op", "request_id"))
        identifier(operation["request_id"])
    elif op == "ro_source_view":
        exact_keys(operation, ("op", "package_id", "package_version", "manifest_digest",
                               "source_id", "selector"))
        for key in ("package_id", "package_version", "source_id"):
            identifier(operation[key])
        safe_text(operation["selector"], maximum=512)
    elif op == "ro_session_state":
        exact_keys(operation, ("op", "task_id", "session_id"))
        identifier(operation["task_id"])
        identifier(operation["session_id"])
    else:
        raise HomeError("OPERATION_NOT_IN_SLICE")


def _safe_reason(exc):
    code = str(exc)
    if code in {
        "GRANT_REVOKED", "GRANT_NOT_ACTIVE", "REVOCATION_EPOCH_ROLLBACK",
        "OPERATION_NOT_GRANTED", "PACKAGE_NOT_ACTIVE", "PACKAGE_BINDING_MISMATCH",
        "GRANT_PACKAGE_BINDING_MISMATCH", "GRANT_SCOPE_MISMATCH",
        "SOURCE_EXPIRED", "READONLY_GRANT_REQUIRED",
    }:
        return code
    return "READONLY_ACCESS_DENIED"


def _classify_local(local_value, timezone_name):
    try:
        zone = ZoneInfo(timezone_name)
        naive = datetime.fromisoformat(local_value)
    except (ValueError, ZoneInfoNotFoundError):
        return "INVALID", None
    if naive.tzinfo is not None:
        return "INVALID", None
    candidates = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        back = aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if back == naive:
            candidates.append(aware)
    offsets = {c.utcoffset() for c in candidates}
    if not candidates:
        return "NONEXISTENT", None
    if len(offsets) > 1:
        return "AMBIGUOUS", None
    return "VALID", candidates[0]


def evaluate_life_rule(text, now):
    try:
        rule = json.loads(text)
    except json.JSONDecodeError:
        return {"status": "HOLD", "reason": "INVALID_TIME_RULE"}
    if not isinstance(rule, dict):
        return {"status": "HOLD", "reason": "INVALID_TIME_RULE"}
    fields = {"timezone", "start_minute", "end_minute", "effective_from", "expires_at",
              "message", "exception_local"}
    if set(rule) - fields or not {"timezone", "start_minute", "end_minute", "effective_from",
                                 "expires_at", "message"}.issubset(rule):
        return {"status": "HOLD", "reason": "INVALID_TIME_RULE"}
    try:
        zone = ZoneInfo(rule["timezone"])
        moment = _now(now)
        start = _now(rule["effective_from"])
        expires = _now(rule["expires_at"])
    except (HomeError, ZoneInfoNotFoundError, TypeError):
        return {"status": "HOLD", "reason": "UNKNOWN_CLOCK"}
    if type(rule["start_minute"]) is not int or type(rule["end_minute"]) is not int:
        return {"status": "HOLD", "reason": "INVALID_TIME_RULE"}
    if not 0 <= rule["start_minute"] < 1440 or not 0 <= rule["end_minute"] < 1440:
        return {"status": "HOLD", "reason": "INVALID_TIME_RULE"}
    if not start <= moment < expires:
        return {"status": "INACTIVE", "reason": "RULE_OUTSIDE_LIFETIME"}
    if rule.get("exception_local"):
        state, _ = _classify_local(rule["exception_local"], rule["timezone"])
        if state == "AMBIGUOUS":
            return {"status": "HOLD", "reason": "LOCAL_TIME_AMBIGUOUS"}
        if state == "NONEXISTENT":
            return {"status": "HOLD", "reason": "LOCAL_TIME_NONEXISTENT"}
        if state != "VALID":
            return {"status": "HOLD", "reason": "INVALID_TIME_RULE"}
    local = moment.astimezone(zone)
    minute = local.hour * 60 + local.minute
    start_minute, end_minute = rule["start_minute"], rule["end_minute"]
    active = (start_minute <= minute < end_minute if start_minute <= end_minute
              else minute >= start_minute or minute < end_minute)
    return {
        "status": "ACTIVE" if active else "INACTIVE",
        "reason": "LOCAL_BOUNDARY_ACTIVE" if active else "OUTSIDE_LOCAL_BOUNDARY",
        "message": rule["message"] if active else None,
        "timezone": rule["timezone"], "local_minute": minute,
    }


class ReadonlySession:
    def __init__(self, runtime, *, bundle_root=None, readonly_grants=(), readonly_now=None,
                 fault=None):
        self.runtime = runtime
        self.scope = runtime.scope
        self.bundle_root = Path(bundle_root).absolute() if bundle_root is not None else None
        self.grants = tuple(readonly_grants)
        self.now = readonly_now
        self.fault = fault
        self.store = runtime.directory / "readonly"
        self.store.mkdir(mode=0o700, exist_ok=True)
        self.control_path = self.store / "control.json"
        self.counters = {
            "package_validations": 0, "package_ingests": 0, "source_reads": 0,
            "local_control_writes": 0, "provider_invocations": 0,
            "real_provider_invocations": 0, "external_connectors": 0,
            "external_side_effects": 0, "authority_writes": 0, "notifications": 0,
        }

    def _fault(self, name):
        if self.fault == name:
            os._exit(86)

    def _control(self):
        if not self.control_path.exists():
            return {"epochs": {}, "anchors": {}}
        try:
            value = json.loads(self.control_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            raise HomeError("READONLY_CONTROL_CORRUPT") from None
        if not isinstance(value, dict) or set(value) != {"epochs", "anchors"}:
            raise HomeError("READONLY_CONTROL_CORRUPT")
        return value

    def _save_control(self, value):
        temp = self.control_path.with_name("control.json.tmp-" + os.urandom(6).hex())
        data = encode(value).encode("utf-8")
        with open(temp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.control_path)
        self.counters["local_control_writes"] += 1

    @contextmanager
    def _request_lock(self, request_id):
        identifier(request_id)
        lock_dir = self.store / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        with open(lock_dir / (fingerprint(request_id) + ".lock"), "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _manifest(self):
        if self.bundle_root is None:
            raise HomeError("READONLY_BUNDLE_REQUIRED")
        return read_manifest(self.bundle_root)

    def _grant(self, manifest, operation):
        control = self._control()
        candidates = [
            g for g in self.grants
            if isinstance(g, dict) and
            g.get("package_id") == manifest["package_id"] and
            g.get("package_version") == manifest["package_version"] and
            g.get("manifest_digest") == manifest["manifest_digest"] and
            g.get("universe_id") == self.scope.universe_id and
            g.get("access_subject_id") == self.scope.access_subject_id
        ]
        if len(candidates) != 1:
            raise HomeError("READONLY_GRANT_REQUIRED")
        grant = candidates[0]
        minimum = control["epochs"].get(grant.get("grant_id"))
        ref = validate_grant(grant, manifest, self.now, operation=operation,
                             minimum_epoch=minimum)
        if minimum is None or grant["revocation_epoch"] > minimum:
            control["epochs"][grant["grant_id"]] = grant["revocation_epoch"]
            self._save_control(control)
        return grant, ref

    def _checked_input(self, operation):
        manifest = self._manifest()
        grant, ref = self._grant(manifest, operation)
        self.counters["package_validations"] += 1
        checked = validate_bundle(self.bundle_root, grant, self.now, operation=operation,
                                  minimum_epoch=self._control()["epochs"].get(grant["grant_id"]))
        checked["grant"] = grant
        checked["grant_ref"] = ref
        return checked

    def _active(self, package_id, package_version, digest, operation):
        manifest = self._manifest()
        if (manifest["package_id"], manifest["package_version"], manifest["manifest_digest"]) != (
                package_id, package_version, digest):
            raise HomeError("PACKAGE_BINDING_MISMATCH")
        grant, ref = self._grant(manifest, operation)
        checked = load_active(self.store, package_id, package_version, digest, grant,
                              self.now, operation=operation)
        checked["grant"] = grant
        checked["grant_ref"] = ref
        return checked

    @staticmethod
    def _extension(event):
        return event["metadata"]["extensions"]["owned_home_readonly"]

    def _events(self, request_id=None):
        result = []
        for event in self.runtime.journal.export():
            extension = event.get("metadata", {}).get("extensions", {}).get("owned_home_readonly")
            if not extension or extension.get("profile_version") != READONLY_PROFILE:
                continue
            if extension.get("scope") != self.scope.projection():
                continue
            if request_id is None or extension["identity"]["request_id"] == request_id:
                result.append(event)
        return sorted(result, key=lambda e: (e["session_id"], e["sequence_no"], e["event_id"]))

    def _identity(self, turn):
        key = fingerprint({k: turn[k] for k in (
            "task_id", "session_id", "request_id", "turn_id",
            "universe_id", "access_subject_id"
        )})
        legacy = fingerprint({k: turn[k] for k in (
            "request_id", "session_id", "turn_id", "universe_id", "access_subject_id"
        )})
        return {
            "task_id": turn["task_id"], "a019_task_id": "task-" + legacy[:32],
            "request_id": turn["request_id"], "session_id": turn["session_id"],
            "turn_id": turn["turn_id"], "trace_id": "ro-trace-" + key[:32],
            "correlation_id": turn["request_id"],
            "user_event_id": "oh-ro-user-" + key,
            "assistant_event_id": "oh-ro-asst-" + key,
            "attempt_id": "oh-ro-attempt-" + key,
        }

    def _request_projection(self, turn):
        return {k: v for k, v in turn.items() if k != "question"}

    def _event(self, turn, actor, projection=None):
        identity = self._identity(turn)
        control = {
            "profile_version": READONLY_PROFILE, "scope": self.scope.projection(),
            "identity": identity, "request": self._request_projection(turn),
            "request_fingerprint": fingerprint(turn),
            "package": {
                "package_id": turn["package_id"], "package_version": turn["package_version"],
                "manifest_digest": turn["manifest_digest"],
            },
        }
        if projection is not None:
            control["projection"] = projection
        event_id = identity["user_event_id" if actor == "user" else "assistant_event_id"]
        return {
            "event_id": event_id,
            "session_id": "oh-ro-session-" + fingerprint({
                **self.scope.projection(), "task_id": turn["task_id"],
                "session_id": turn["session_id"],
            }),
            "turn_id": turn["turn_id"],
            "sequence_no": turn["turn_no"] * 2 - (1 if actor == "user" else 0),
            "actor_role": actor, "message_id": event_id,
            "persona_id": "synthetic-readonly-persona", "relationship_id": None,
            "provider": "offline-stub", "model": "deterministic-readonly-v1",
            "observed_at": self.now, "created_at": self.now,
            "content_type": "text/plain",
            "content_payload": {"text": turn["question"] if actor == "user" else ""},
            "status": "complete",
            "source_ref": {
                "source_kind": "owned_client", "observation_type": "observed",
                "source_id": turn["request_id"], "uri": None,
                "adapter_version": READONLY_PROFILE,
            },
            "attachment_ref": [], "correction_id": None, "correction_of": None,
            "redaction_state": "none",
            "metadata": {
                "adapter": "A019", "adapter_version": READONLY_PROFILE,
                "ingest_id": event_id, "knowledge": {},
                "extensions": {"owned_home_readonly": control},
            },
        }

    def _lookup(self, request_id):
        rows = self._events(request_id)
        users = [e for e in rows if e["actor_role"] == "user"]
        terminals = [e for e in rows if e["actor_role"] == "assistant"]
        if len(users) > 1 or len(terminals) > 1:
            raise HomeError("DUPLICATE_CANONICAL_IDENTITY")
        return users, terminals

    def _source_ref(self, source, text):
        return {
            "source_id": source["source_id"], "source_revision": source["source_revision"],
            "content_digest": source["content_digest"], "selector": source["selector"],
            "char_span": [0, len(text)], "role": source["role"], "as_of": source["as_of"],
            "coverage": source["coverage"], "owning_authority": source["owning_authority"],
        }

    @staticmethod
    def _resolve_group(group):
        if len(group) <= 1:
            return group, None
        by_id = {s["source_id"]: s for s in group}
        superseders = [s for s in group if s.get("supersession_ref") in by_id]
        if len(superseders) == 1:
            return superseders, None
        return [], {
            "selector": group[0]["selector"], "reason": "AMBIGUOUS_AUTHORITY",
            "source_ids": sorted(s["source_id"] for s in group),
        }

    def _context(self, turn, checked):
        manifest, payloads = checked["manifest"], checked["payloads"]
        sources = {s["source_id"]: s for s in manifest["sources"]}
        requested = turn["evidence_needs"] or [
            {"source_id": source_id, "selector": sources[source_id]["selector"]}
            for source_id in manifest["required_source_ids"]
        ]
        required = set(manifest["required_source_ids"])
        selected, omitted = [], []
        for need in requested:
            source = sources.get(need["source_id"])
            if source is None or source["selector"] != need["selector"]:
                omitted.append({"source_id": need["source_id"], "reason": "NOT_LOOKED_UP"})
                continue
            if source["coverage"] == "NOT_LOADED":
                omitted.append({"source_id": source["source_id"], "reason": "NOT_LOADED"})
                continue
            text = payloads.get(source["source_id"])
            if text is None:
                omitted.append({"source_id": source["source_id"], "reason": "UNKNOWN"})
                continue
            selected.append((source, text))
        conflicts = []
        grouped = {}
        for source, text in selected:
            if source["role"] == "AUTHORITY":
                grouped.setdefault(source["selector"], []).append(source)
        blocked_ids = set()
        for group in grouped.values():
            resolved, conflict = self._resolve_group(group)
            if conflict:
                conflicts.append(conflict)
                blocked_ids.update(s["source_id"] for s in group)
            elif resolved:
                keep = {s["source_id"] for s in resolved}
                blocked_ids.update(s["source_id"] for s in group if s["source_id"] not in keep)
        selected = [(s, t) for s, t in selected if s["source_id"] not in blocked_ids]
        evidence = []
        for source, text in selected:
            evidence.append({"ref": self._source_ref(source, text), "text": text})
        cognition = {"question": turn["question"], "evidence": evidence}
        required_bytes = len(encode(cognition).encode("utf-8"))
        stop_reason = None
        if conflicts:
            stop_reason = "AUTHORITY_CONFLICT"
        missing_required = [
            item for item in omitted if item["source_id"] in required
        ]
        for source, _ in selected:
            if source["source_id"] in required and source["coverage"] == "MISSING_ATTACHMENT":
                missing_required.append({"source_id": source["source_id"], "reason": "MISSING_ATTACHMENT"})
        if missing_required:
            stop_reason = stop_reason or "REQUIRED_EVIDENCE_UNAVAILABLE"
        if required_bytes > turn["budget_bytes"]:
            omitted.extend({"source_id": e["ref"]["source_id"], "reason": "CONTEXT_BUDGET_EXCEEDED"}
                           for e in evidence)
            evidence = []
            stop_reason = "CONTEXT_BUDGET_EXCEEDED"
        known_empty = bool(evidence) and all(e["text"] == "" for e in evidence)
        answerability = (
            "UNKNOWN" if stop_reason else
            "KNOWN_EMPTY" if known_empty else
            "KNOWN_VALUE" if evidence else
            "NOT_LOOKED_UP"
        )
        life_hint = None
        for source, text in selected:
            if source["source_kind"] == "life_time_rule":
                life_hint = evaluate_life_rule(text, self.now)
                if life_hint["status"] == "HOLD":
                    stop_reason = life_hint["reason"]
                    answerability = "UNKNOWN"
        as_values = sorted(e["ref"]["as_of"] for e in evidence)
        envelope = {
            "profile_version": READONLY_PROFILE, "answer_version": ANSWER_VERSION,
            "task_id": turn["task_id"], "session_id": turn["session_id"],
            "request_id": turn["request_id"], "package_id": turn["package_id"],
            "package_version": turn["package_version"],
            "manifest_digest": turn["manifest_digest"],
            "grant_ref": checked["grant_ref"], "status": "HOLD" if stop_reason else "READY",
            "answerability": answerability, "time_basis": "AS_OF",
            "as_of": as_values[-1] if as_values else None,
            "source_refs": [e["ref"] for e in evidence],
            "conflicts": conflicts, "omitted_required_evidence": missing_required,
            "synthetic_cognition": True, "authorization_effect": "NONE",
            "local_time_hint": life_hint, "stop_reason": stop_reason,
            "budget": {
                "unit": "UTF8_COGNITION_INPUT_BYTES", "limit": turn["budget_bytes"],
                "required": required_bytes, "included_bytes": 0 if stop_reason == "CONTEXT_BUDGET_EXCEEDED" else required_bytes,
                "silent_truncations": 0,
            },
            "receipts": {}, "safe_counters": dict(self.counters),
        }
        envelope["trace"] = readonly_trace(envelope)
        if stop_reason:
            reply = "Stopped: " + stop_reason
        elif known_empty:
            reply = "Synthetic read-only source is explicitly empty."
        else:
            reply = "Synthetic read-only evidence: " + " | ".join(
                f"[{e['ref']['source_id']}] {e['text']}" for e in evidence
            )
        return envelope, reply

    def _remember_anchor(self, turn, terminal):
        control = self._control()
        key = fingerprint({
            "task_id": turn["task_id"], "session_id": turn["session_id"],
            "universe_id": turn["universe_id"],
            "access_subject_id": turn["access_subject_id"],
        })
        identity = self._identity(turn)
        control["anchors"][key] = {
            "resume_version": RESUME_VERSION, "task_id": turn["task_id"],
            "session_id": turn["session_id"], "universe_id": turn["universe_id"],
            "access_subject_id": turn["access_subject_id"],
            "package_id": turn["package_id"], "package_version": turn["package_version"],
            "manifest_digest": turn["manifest_digest"],
            "grant_ref": terminal["metadata"]["extensions"]["owned_home_readonly"]["projection"]["grant_ref"],
            "last_user_event_ref": identity["user_event_id"],
            "last_assistant_event_ref": identity["assistant_event_id"],
            "context_version": CONTEXT_VERSION,
        }
        self._save_control(control)

    def info(self):
        manifest = read_manifest(self.bundle_root) if self.bundle_root is not None else None
        return {
            "profile_version": READONLY_PROFILE, "contract_version": VERSION,
            "offline_only": True, "synthetic_only": True, "live_provider_enabled": False,
            "external_connectors_enabled": False, "model_egress_allowed": False,
            "supported_ops": list(READONLY_OPS), "fault_points": sorted(READONLY_FAULTS),
            "limits": {"sources": 16, "source_bytes": 32768, "total_payload_bytes": 524288,
                       "request_bytes": 1048576, "evidence_needs": 5,
                       "recent_turns": 4, "context_budget_min": 64,
                       "context_budget_max": 65536},
            "configured_package": None if manifest is None else {
                "package_id": manifest["package_id"], "package_version": manifest["package_version"],
                "manifest_digest": manifest["manifest_digest"], "task_id": manifest["task_id"],
            },
        }

    def package_validate(self):
        checked = self._checked_input("ro_package_validate")
        return {
            "status": "VALID", "package_id": checked["manifest"]["package_id"],
            "package_version": checked["manifest"]["package_version"],
            "manifest_digest": checked["manifest"]["manifest_digest"],
            "total_payload_bytes": checked["total_payload_bytes"],
            "grant_ref": checked["grant_ref"],
        }

    def package_ingest(self):
        checked = self._checked_input("ro_package_ingest")
        result = activate_bundle(self.bundle_root, self.store, checked["grant"], self.now,
                                 fault=self.fault)
        self.counters["package_ingests"] += 1
        result["safe_counters"] = dict(self.counters)
        return result

    def turn(self, turn, *, resume=False):
        turn = _turn(turn)
        if (turn["universe_id"], turn["access_subject_id"]) != (
                self.scope.universe_id, self.scope.access_subject_id):
            raise HomeError("SCOPE_DENIED")
        checked = self._active(turn["package_id"], turn["package_version"],
                               turn["manifest_digest"], "ro_turn")
        if checked["manifest"]["task_id"] != turn["task_id"]:
            raise HomeError("TASK_BINDING_MISMATCH")
        with self._request_lock(turn["request_id"]):
            users, terminals = self._lookup(turn["request_id"])
            if users:
                saved = self._extension(users[0])
                if saved["request_fingerprint"] != fingerprint(turn):
                    raise HomeError("REQUEST_IDENTITY_CONFLICT")
                if terminals or not resume:
                    return self.observe(turn["request_id"])
            elif resume:
                raise HomeError("RESUME_TARGET_MISSING")
            session_users = [
                e for e in self._events()
                if e["actor_role"] == "user" and
                self._extension(e)["identity"]["task_id"] == turn["task_id"] and
                self._extension(e)["identity"]["session_id"] == turn["session_id"]
            ]
            if not users:
                expected = max((e["sequence_no"] + 1) // 2 for e in session_users) + 1 if session_users else 1
                if turn["turn_no"] != expected:
                    raise HomeError("SESSION_SEQUENCE_CONFLICT")
                for prior in session_users:
                    prior_id = self._extension(prior)["identity"]["request_id"]
                    _, prior_terminals = self._lookup(prior_id)
                    if not prior_terminals:
                        raise HomeError("PENDING_TURN_REQUIRES_RESUME")
            user = self._event(turn, "user")
            user_receipt = self.runtime.journal.ingest("A019", user)
            if self.runtime.fault == "AFTER_USER_DURABLE":
                os._exit(86)
            envelope, reply = self._context(turn, checked)
            self._fault("RO_AFTER_CONTEXT")
            template = self._event(turn, "assistant", envelope)
            outcome = "failed" if envelope["stop_reason"] else "complete"
            receipt = self.runtime.journal.append_user_then_invoke_stub(
                user, template, attempt_id=self._identity(turn)["attempt_id"],
                script=StubScript((reply,), outcome),
            )
            self.runtime.counters["cognition_stub_invocations"] += receipt["provider_invocations"]
            result = self.observe(turn["request_id"])
            result["receipts"]["user"] = user_receipt
            result["receipts"]["assistant"] = receipt["assistant"]
            result["execution_order"] = [
                "USER_DURABLE_RECEIPT", "READONLY_GRANT_ALLOW",
                "READONLY_CONTEXT_" + ("BLOCKED" if envelope["stop_reason"] else "READY"),
                *receipt["trace"], "DISPLAY",
            ]
            _, terminals = self._lookup(turn["request_id"])
            if terminals:
                self._remember_anchor(turn, terminals[0])
            return result

    def observe(self, request_id):
        identifier(request_id)
        users, terminals = self._lookup(request_id)
        if not users:
            return {
                "profile_version": READONLY_PROFILE, "request_id": request_id,
                "status": "NOT_FOUND", "answerability": "UNKNOWN",
                "visible_reply": None, "real_provider_invocations": 0,
                "external_side_effects": 0,
            }
        control = self._extension(users[0])
        request = control["request"]
        try:
            checked = self._active(
                request["package_id"], request["package_version"],
                request["manifest_digest"], "ro_observe",
            )
        except HomeError as exc:
            return {
                "profile_version": READONLY_PROFILE, "request_id": request_id,
                "status": "HOLD", "answerability": "UNKNOWN",
                "stop_reason": _safe_reason(exc), "visible_reply": None,
                "source_refs": [], "conflicts": [], "receipts": {},
                "safe_counters": dict(self.counters), "real_provider_invocations": 0,
                "external_side_effects": 0,
            }
        result = {
            "profile_version": READONLY_PROFILE, **control["identity"],
            **self.scope.projection(), "status": "AWAIT_EXPLICIT_RESUME",
            "answerability": "UNKNOWN", "visible_reply": None,
            "stop_reason": "EXPLICIT_RESUME_REQUIRED", "terminal_count": len(terminals),
            "event_count": len(users) + len(terminals),
            "receipts": {"user": self.runtime.journal.ingest("A019", users[0])},
            "safe_counters": dict(self.counters), "real_provider_invocations": 0,
            "external_side_effects": 0,
        }
        if terminals:
            terminal = terminals[0]
            saved = self._extension(terminal)["projection"]
            result.update(saved)
            result["status"] = terminal["status"]
            result["visible_reply"] = terminal["content_payload"].get("text", "")
            result["terminal_count"] = 1
            result["receipts"] = {
                "user": result["receipts"]["user"],
                "assistant": self.runtime.journal.ingest("A019", terminal),
            }
            result["safe_counters"] = dict(self.counters)
        return result

    def resume(self, request_id):
        users, _ = self._lookup(request_id)
        if not users:
            raise HomeError("RESUME_TARGET_MISSING")
        control = self._extension(users[0])
        turn = dict(control["request"])
        turn["question"] = users[0]["content_payload"]["text"]
        return self.turn(turn, resume=True)

    def source_view(self, operation):
        checked = self._active(operation["package_id"], operation["package_version"],
                               operation["manifest_digest"], "ro_source_view")
        for source in checked["manifest"]["sources"]:
            if source["source_id"] == operation["source_id"] and source["selector"] == operation["selector"]:
                if not source["scope"]["display_allowed"]:
                    raise HomeError("DISPLAY_NOT_ALLOWED")
                if source["coverage"] == "NOT_LOADED":
                    return {"status": "UNKNOWN", "reason": "NOT_LOADED", "text": None}
                self.counters["source_reads"] += 1
                text = checked["payloads"][source["source_id"]]
                return {"status": "KNOWN_EMPTY" if text == "" else "KNOWN_VALUE",
                        "text": text, "source_ref": self._source_ref(source, text),
                        "safe_counters": dict(self.counters)}
        return {"status": "NOT_LOOKED_UP", "text": None}

    def session_state(self, task_id, session_id):
        rows = [
            e for e in self._events()
            if e["actor_role"] == "user" and
            self._extension(e)["identity"]["task_id"] == task_id and
            self._extension(e)["identity"]["session_id"] == session_id
        ]
        if not rows:
            return {"profile_version": READONLY_PROFILE, "task_id": task_id,
                    "session_id": session_id, "status": "UNKNOWN", "requests": [],
                    "next_turn_no": 1}
        packages = {
            (self._extension(e)["request"]["package_id"],
             self._extension(e)["request"]["package_version"],
             self._extension(e)["request"]["manifest_digest"])
            for e in rows
        }
        return {
            "profile_version": READONLY_PROFILE, "task_id": task_id,
            "session_id": session_id, "status": "HOLD" if len(packages) > 1 else "KNOWN_VALUE",
            "reason": "AMBIGUOUS_PACKAGE_HISTORY" if len(packages) > 1 else None,
            "requests": [self._extension(e)["identity"]["request_id"] for e in rows],
            "next_turn_no": max((e["sequence_no"] + 1) // 2 for e in rows) + 1,
            "packages": [list(p) for p in sorted(packages)],
        }

    def safe_export(self):
        exported = []
        for event in self._events():
            extension = self._extension(event)
            projection = extension.get("projection", {})
            exported.append({
                "event_id": event["event_id"], "actor_role": event["actor_role"],
                "sequence_no": event["sequence_no"],
                "request_id": extension["identity"]["request_id"],
                "task_id": extension["identity"]["task_id"],
                "session_id": extension["identity"]["session_id"],
                "package": extension["package"],
                "status": event["status"], "answerability": projection.get("answerability"),
                "source_refs": projection.get("source_refs", []),
                "stop_reason": projection.get("stop_reason"),
                "receipt": self.runtime.journal.ingest("A019", event),
            })
        return {"profile_version": READONLY_PROFILE, "events": exported,
                "safe_counters": dict(self.counters)}

    def execute(self, operation):
        validate_readonly_operation(operation)
        op = operation["op"]
        if op == "ro_info":
            return self.info()
        if op == "ro_package_validate":
            return self.package_validate()
        if op == "ro_package_ingest":
            return self.package_ingest()
        if op == "ro_turn":
            return self.turn(operation["turn"])
        if op == "ro_observe":
            return self.observe(operation["request_id"])
        if op == "ro_resume":
            return self.resume(operation["request_id"])
        if op == "ro_source_view":
            return self.source_view(operation)
        if op == "ro_session_state":
            return self.session_state(operation["task_id"], operation["session_id"])
        if op == "ro_safe_export":
            return self.safe_export()
        raise HomeError("OPERATION_NOT_IN_SLICE")


def execute_readonly_request(directory, request, *, fault=None):
    from .runtime import FAULTS, OwnedRuntime

    if not isinstance(request, dict):
        raise HomeError("INVALID_SHAPE")
    required = {"contract_version", "profile_version", "scope", "op"}
    setup = {"readonly_bundle_root", "readonly_grants", "readonly_now"}
    operation_keys = {
        "turn", "request_id", "package_id", "package_version", "manifest_digest",
        "source_id", "selector", "task_id", "session_id",
    }
    if set(request) - required - setup - operation_keys or required - set(request):
        raise HomeError("INVALID_SHAPE")
    if request["contract_version"] != VERSION:
        raise HomeError("CONTRACT_VERSION_MISMATCH")
    if request["profile_version"] != READONLY_PROFILE:
        raise HomeError("PROFILE_VERSION_MISMATCH")
    scope = Scope(**request["scope"])
    grants = request.get("readonly_grants")
    if not isinstance(grants, list) or len(grants) > 16:
        raise HomeError("INVALID_READONLY_GRANTS")
    now = request.get("readonly_now")
    if request["op"] != "ro_info" and now is None:
        raise HomeError("READONLY_CLOCK_REQUIRED")
    operation = {k: v for k, v in request.items()
                 if k not in required - {"op"} and k not in setup and k != "profile_version"}
    operation["op"] = request["op"]
    validate_readonly_operation(operation)
    runtime_fault = fault if fault in FAULTS else None
    with OwnedRuntime(directory, scope=scope, fault=runtime_fault) as runtime:
        session = runtime.readonly_session(
            bundle_root=request.get("readonly_bundle_root"),
            readonly_grants=grants, readonly_now=now, fault=fault,
        )
        return session.execute(operation)
