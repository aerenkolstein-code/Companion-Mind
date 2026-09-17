"""OwnedHomeTestPort v1: bounded synthetic-only P2-S1 contracts.

No credentials, production sources, permission escalation or live adapters.
Unlabelled arbitrary secrets cannot be recognized: fixtures must be explicitly
public-safe and synthetic. Declared credential patterns are rejected, not echoed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
import re

VERSION = "owned-home/1"
BASE_SHA = "0cf97de23a8725cded403a58cd8d85eb35b67b08"
BASE_TREE = "45a7426ac4af6f461b182375cb007677bf5c433f"
SECRET = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|password|passwd|secret|access[_-]?token|"
    r"refresh[_-]?token|authorization)\s*[:=]\s*\S+|\bBearer\s+\S+|"
    r"\bsk-[A-Za-z0-9_-]{8,}|\bgh[pousr]_[A-Za-z0-9]{8,}|"
    r"-----BEGIN[^\n]*PRIVATE KEY-----)"
)


class HomeError(ValueError):
    """Only fixed safe error codes cross the shell/TestPort boundary."""


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def safe_text(value, *, maximum=32768):
    if not isinstance(value, str):
        raise HomeError("INVALID_TEXT")
    if len(value.encode("utf-8")) > maximum:
        raise HomeError("INPUT_TOO_LARGE")
    if SECRET.search(value) or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise HomeError("UNSAFE_INPUT")
    return value


def identifier(value):
    safe_text(value, maximum=80)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value):
        raise HomeError("INVALID_ID")
    return value


def exact_keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise HomeError("INVALID_SHAPE")


def timestamp(value):
    safe_text(value, maximum=40)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise HomeError("INVALID_TIMESTAMP") from None
    return value


@dataclass(frozen=True)
class Scope:
    universe_id: str
    access_subject_id: str

    def __post_init__(self):
        identifier(self.universe_id)
        identifier(self.access_subject_id)

    def projection(self):
        return asdict(self)


@dataclass(frozen=True)
class AuthorityFixture:
    source_id: str
    version: str
    universe_id: str
    access_subject_id: str
    text: str
    observed_at: str = "2026-09-16T00:00:00+00:00"
    synthetic: bool = True
    public_safe: bool = True

    def __post_init__(self):
        for value in (self.source_id, self.version, self.universe_id, self.access_subject_id):
            identifier(value)
        safe_text(self.text)
        timestamp(self.observed_at)
        if self.synthetic is not True or self.public_safe is not True:
            raise HomeError("PUBLIC_SYNTHETIC_FIXTURE_REQUIRED")

    def ref(self):
        return {"source_id": self.source_id, "version": self.version,
                "authority_class": "SYNTHETIC_LOCAL", "status": "CURRENT",
                "universe_id": self.universe_id, "access_subject_id": self.access_subject_id,
                "observed_at": self.observed_at, "content_fingerprint": fingerprint(self.text)}


@dataclass(frozen=True)
class Grant:
    universe_id: str
    access_subject_id: str
    source_id: str
    version: str
    decision: str = "ALLOW"

    def __post_init__(self):
        for value in (self.universe_id, self.access_subject_id, self.source_id, self.version):
            identifier(value)
        if self.decision not in {"ALLOW", "DENY", "UNKNOWN"}:
            raise HomeError("INVALID_PERMISSION")


@dataclass(frozen=True)
class Turn:
    request_id: str
    session_id: str
    turn_id: str
    turn_no: int
    universe_id: str
    access_subject_id: str
    source_id: str
    source_version: str
    text: str
    observed_at: str
    budget_bytes: int = 4096
    contract_version: str = VERSION

    def __post_init__(self):
        for value in (self.request_id, self.session_id, self.turn_id, self.universe_id,
                      self.access_subject_id, self.source_id, self.source_version):
            identifier(value)
        safe_text(self.text)
        timestamp(self.observed_at)
        if type(self.turn_no) is not int or not 1 <= self.turn_no <= 1_000_000:
            raise HomeError("INVALID_TURN_NUMBER")
        if type(self.budget_bytes) is not int or not 64 <= self.budget_bytes <= 65536:
            raise HomeError("INVALID_BUDGET")
        if self.contract_version != VERSION:
            raise HomeError("CONTRACT_VERSION_MISMATCH")

    @property
    def scope(self):
        return Scope(self.universe_id, self.access_subject_id)

    @property
    def identity(self):
        # Transport, wall-clock time, process, provider and display labels are absent.
        key = fingerprint({k: getattr(self, k) for k in (
            "request_id", "session_id", "turn_id", "universe_id", "access_subject_id")})
        return {"request_id": self.request_id, "session_id": self.session_id,
                "turn_id": self.turn_id, "trace_id": "trace-" + key[:32],
                "correlation_id": self.request_id, "task_id": "task-" + key[:32],
                "user_event_id": "oh-user-" + key, "assistant_event_id": "oh-asst-" + key,
                "attempt_id": "oh-attempt-" + key}

    def projection(self):
        return asdict(self)


def permission(scope, source_id, version, grants):
    """Grant lookup uses identity metadata only, before consulting any source."""
    identifier(source_id)
    identifier(version)
    matching = [g for g in grants if (g.universe_id, g.access_subject_id, g.source_id, g.version)
                == (scope.universe_id, scope.access_subject_id, source_id, version)]
    states = {g.decision for g in matching}
    allowed = states == {"ALLOW"}
    result = {"contract_version": VERSION, **scope.projection(), "source_id": source_id,
              "source_version": version, "tier": "P1_SCOPED_READ",
              "decision": "ALLOW" if allowed else "DENY",
              "knowledge_state": "KNOWN_VALUE" if matching and "UNKNOWN" not in states else "UNKNOWN",
              "reason": "EXPLICIT_GRANT" if allowed else "DEFAULT_DENY_OR_CONFLICT",
              "policy_fingerprint": fingerprint([asdict(g) for g in sorted(
                  matching, key=lambda g: (g.decision, g.version))])}
    result["decision_fingerprint"] = fingerprint(result)
    return result


@dataclass(frozen=True)
class WakeCandidate:
    event_id: str
    universe_id: str
    access_subject_id: str
    owner_subject_id: str
    observed_at: str
    salience: float = 0.5
    urgency: float = 0.5
    confidence: float = 1.0
    quiet_hours: bool = False
    cooldown_remaining: int = 0
    repeat_count: int = 0
    repeat_limit: int = 1

    def __post_init__(self):
        for value in (self.event_id, self.universe_id, self.access_subject_id, self.owner_subject_id):
            identifier(value)
        timestamp(self.observed_at)
        for value in (self.salience, self.urgency, self.confidence):
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
                raise HomeError("INVALID_WAKE_SCORE")
        if type(self.quiet_hours) is not bool:
            raise HomeError("INVALID_QUIET_HOURS")
        for value in (self.cooldown_remaining, self.repeat_count, self.repeat_limit):
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise HomeError("INVALID_WAKE_LIMIT")


def evaluate_wake(candidate, scope):
    checks = {"scope": (candidate.universe_id, candidate.access_subject_id)
              == (scope.universe_id, scope.access_subject_id),
              "salience": candidate.salience >= 0.5, "urgency": candidate.urgency >= 0.5,
              "confidence": candidate.confidence >= 0.5,
              "quiet_hours": not candidate.quiet_hours, "cooldown": candidate.cooldown_remaining == 0,
              "repeat_limit": candidate.repeat_count < candidate.repeat_limit,
              "owner": candidate.owner_subject_id == scope.access_subject_id}
    result = {"contract_version": VERSION, "event_id": candidate.event_id,
              **scope.projection(), "checks": checks, "baseline": asdict(candidate),
              "owner_result": "UNIQUE_OWNER" if checks["owner"] and checks["scope"] else "HOLD_CONFLICT",
              "action": "SILENT", "reason": "SLICE1_SILENT_ONLY",
              "suppressed_by": [k for k, v in checks.items() if not v],
              "notifications": 0, "continuations": 0, "external_side_effects": 0}
    result["decision_fingerprint"] = fingerprint(result)
    return result
