"""Versioned public-synthetic human contracts and bounded mutable Runtime control.

Raw response evidence belongs to A019. This snapshot contains only identities,
fingerprints, derived command and execution control; it is not an event history.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .action_control import AtomicState
from .contracts import HomeError, Scope, fingerprint, identifier, safe_text, timestamp
from .continuation import DEFAULT_BUDGET, validate_budget

NOW = "2026-09-18T00:00:00+00:00"
IDENTITY = ("human_request_id", "request_id", "trace_id", "goal_id", "task_id",
            "session_id", "turn_id", "turn_no", "universe_id", "access_subject_id", "owner_id")
HUMAN_FAULTS = {"HUMAN_AFTER_REQUEST_CONTROL", "HUMAN_AFTER_REQUEST_DURABLE",
                "HUMAN_AFTER_RESPONSE_EVIDENCE", "HUMAN_AFTER_RESPONSE_DURABLE",
                "HUMAN_AFTER_RESUME_INTENT", "HUMAN_AFTER_CONTINUATION",
                "HUMAN_AFTER_CONTINUATION_RECEIPT", "HUMAN_AFTER_TERMINAL_DURABLE"}


def instant(value):
    timestamp(value)
    return datetime.fromisoformat(value)


def validate_identity(value):
    for key in IDENTITY:
        if key != "turn_no":
            identifier(getattr(value, key))
    if type(value.turn_no) is not int or not 1 <= value.turn_no <= 1_000_000:
        raise HomeError("INVALID_TURN_NUMBER")


@dataclass(frozen=True)
class HumanRequest:
    human_request_id: str
    request_id: str
    trace_id: str
    goal_id: str
    task_id: str
    session_id: str
    turn_id: str
    turn_no: int
    universe_id: str
    access_subject_id: str
    owner_id: str
    created_at: str = NOW
    expires_at: str = "2026-09-19T00:00:00+00:00"
    budget: dict = field(default_factory=lambda: dict(DEFAULT_BUDGET))
    contract_version: str = "human-request/1"
    request_kind: str = "LOCAL_CONTINUE_DECISION"
    synthetic: bool = True
    public_safe: bool = True

    def __post_init__(self):
        validate_identity(self)
        if self.contract_version != "human-request/1":
            raise HomeError("HUMAN_REQUEST_VERSION_MISMATCH")
        if self.request_kind != "LOCAL_CONTINUE_DECISION":
            raise HomeError("HUMAN_REQUEST_KIND_DENIED")
        if self.synthetic is not True or self.public_safe is not True:
            raise HomeError("PUBLIC_SYNTHETIC_FIXTURE_REQUIRED")
        duration = (instant(self.expires_at) - instant(self.created_at)).total_seconds()
        if not 0 < duration <= 86400:
            raise HomeError("INVALID_REQUEST_LIFETIME")
        object.__setattr__(self, "budget", validate_budget(self.budget))

    @property
    def scope(self):
        return Scope(self.universe_id, self.access_subject_id)

    def projection(self):
        value = {**asdict(self), "initial_state": "CREATED", "initial_cancel_state": "NOT_CANCELLED",
                 "allowed_response_schema": "public-command/1:CONTINUE|HOLD|CANCEL|UNSURE|EMPTY",
                 "budget_ref": fingerprint(self.budget)}
        return {**value, "request_fingerprint": fingerprint(value)}


@dataclass(frozen=True)
class HumanResponse:
    human_request_id: str
    request_id: str
    trace_id: str
    goal_id: str
    task_id: str
    session_id: str
    turn_id: str
    turn_no: int
    universe_id: str
    access_subject_id: str
    owner_id: str
    request_fingerprint: str
    response_id: str
    text: str
    contract_version: str = "human-response/1"
    synthetic: bool = True
    public_safe: bool = True

    def __post_init__(self):
        validate_identity(self)
        identifier(self.response_id)
        if (not isinstance(self.request_fingerprint, str) or len(self.request_fingerprint) != 64 or
                any(c not in "0123456789abcdef" for c in self.request_fingerprint)):
            raise HomeError("INVALID_REQUEST_FINGERPRINT")
        if self.contract_version != "human-response/1":
            raise HomeError("HUMAN_RESPONSE_VERSION_MISMATCH")
        if self.synthetic is not True or self.public_safe is not True:
            raise HomeError("PUBLIC_SYNTHETIC_FIXTURE_REQUIRED")
        safe_text(self.text, maximum=64)
        if self.text.strip().upper() not in {"CONTINUE", "HOLD", "CANCEL", "UNSURE", ""}:
            raise HomeError("INVALID_PUBLIC_COMMAND")

    def projection(self):
        raw = fingerprint({"text": self.text})
        derived = {"version": "public-command/1", "command": self.text.strip().upper() or "UNSURE",
                   "derived": True, "source_raw_fingerprint": raw}
        value = {k: v for k, v in asdict(self).items() if k != "text"}
        value.update(raw_payload_fingerprint=raw, normalized=derived, normalized_fingerprint=fingerprint(derived),
                     lifecycle="RECEIVED", raw_evidence_owner="A019")
        return {**value, "response_fingerprint": fingerprint(value)}


@dataclass(frozen=True)
class OwnerFixture:
    owner_id: str
    universe_id: str
    access_subject_id: str
    goal_id: str | None = None
    task_id: str | None = None
    active: bool = True
    synthetic: bool = True
    public_safe: bool = True

    def __post_init__(self):
        for key in ("owner_id", "universe_id", "access_subject_id", "goal_id", "task_id"):
            if getattr(self, key) is not None:
                identifier(getattr(self, key))
        if type(self.active) is not bool or self.synthetic is not True or self.public_safe is not True:
            raise HomeError("PUBLIC_SYNTHETIC_FIXTURE_REQUIRED")


def resolve_owner(request, owners):
    matches = [asdict(o) for o in owners if o.active and all(
        getattr(o, k) == request[k] for k in ("universe_id", "access_subject_id")) and all(
        getattr(o, k) is None or getattr(o, k) == request[k] for k in ("goal_id", "task_id"))]
    # Duplicate entries also mean ambiguity; never silently deduplicate owners.
    unique = len(matches) == 1 and matches[0]["owner_id"] == request["owner_id"]
    value = {"unique": unique, "owner_id": request["owner_id"] if unique else None,
             "candidate_count": len(matches), "fixture_fingerprint": fingerprint(sorted(matches, key=fingerprint)),
             "status": "EXACT_OWNER" if unique else "OWNER_AMBIGUOUS"}
    return {**value, "owner_fingerprint": fingerprint(value)}


class HumanControl:
    def __init__(self, directory):
        self.state = AtomicState(directory, "human-control/1", {"requests": {}})
        if not isinstance(self.state.data.get("requests"), dict):
            self.close()
            raise HomeError("CONTROL_FORMAT_MISMATCH")

    def close(self):
        self.state.close()

    def save(self):
        self.state.save()

    def lookup(self, human_request_id, scope):
        identifier(human_request_id)
        record = self.state.data["requests"].get(human_request_id)
        if record and any(record["request"][k] != v for k, v in scope.items()):
            raise HomeError("SCOPE_DENIED")
        return record

    def bind(self, request, owner, now, wake=None):
        records = self.state.data["requests"]
        prior = records.get(request["human_request_id"])
        if prior:
            if prior["request"] != request or prior["wake"] != wake:
                raise HomeError("HUMAN_REQUEST_CONFLICT")
            return prior, False
        for r in records.values():
            same_scope = all(r["request"][k] == request[k] for k in ("universe_id", "access_subject_id"))
            if same_scope and (r["request"]["request_id"] == request["request_id"] or
                               all(r["request"][k] == request[k] for k in ("goal_id", "task_id"))):
                raise HomeError("HUMAN_TASK_IDENTITY_CONFLICT")
            if wake and r["wake"] and same_scope and r["wake"]["event_id"] == wake["event_id"]:
                raise HomeError("WAKE_IDENTITY_CONFLICT")
        if len(records) >= 1024:
            raise HomeError("HUMAN_CONTROL_LIMIT")
        record = {"request": request, "owner": owner, "wake": wake, "state": "CREATED",
                  "response": None, "reserved": False, "continuation": None, "decision": None,
                  "reason": None, "last_observed_at": now, "response_at": None, "resume_at": None,
                  "terminal_at": None}
        records[request["human_request_id"]] = record
        self.save()
        return record, True
