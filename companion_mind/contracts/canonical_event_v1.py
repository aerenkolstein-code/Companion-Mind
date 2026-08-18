"""Phase-0 Canonical Event / RAW contract v1.

This module is intentionally a contract and conformance surface, not a durable
journal implementation. It performs deterministic semantic validation with
stdlib only so Browser Sidecar, Owned Client and Historical Backfill adapters
can target the same representation before any Phase-1 storage engine exists.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping


STATUSES = frozenset({"complete", "partial", "failed"})
ACTOR_ROLES = frozenset(
    {"user", "assistant", "tool", "system-derived-visible-event"}
)
SOURCE_KINDS = frozenset(
    {
        "owned_client",
        "browser_sidecar",
        "historical_backfill",
        "correction",
        "replay",
        "derived",
    }
)
OBSERVATION_TYPES = frozenset(
    {"observed", "imported", "corrected", "replayed", "inferred", "projected"}
)
KNOWLEDGE_STATES = frozenset(
    {"KNOWN_VALUE", "KNOWN_EMPTY", "UNKNOWN", "N_A", "NOT_LOOKED_UP"}
)
REDACTION_STATES = frozenset({"none", "redacted", "not_applicable"})

_REQUIRED_FIELDS = (
    "event_id",
    "session_id",
    "turn_id",
    "sequence_no",
    "actor_role",
    "message_id",
    "persona_id",
    "relationship_id",
    "provider",
    "model",
    "observed_at",
    "created_at",
    "content_type",
    "content_payload",
    "status",
    "source_ref",
    "attachment_ref",
    "correction_id",
    "correction_of",
    "redaction_state",
    "metadata",
)

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "password",
        "passwd",
        "otp",
        "cookie",
        "cookies",
        "authorization",
        "csrf",
        "csrf_token",
        "card_number",
        "cvv",
        "cvc",
    }
)

_AUTHORITY_MUTATION_KEYS = frozenset(
    {
        "persona_current",
        "relationship_current",
        "persona_biography",
        "relationship_milestone",
        "relationship_upgrade",
        "authority_write",
        "biography_write",
        "current_state_write",
    }
)

_PROVENANCE_MATRIX = {
    "owned_client": frozenset({"observed"}),
    "browser_sidecar": frozenset({"observed"}),
    "historical_backfill": frozenset({"imported"}),
    "correction": frozenset({"corrected"}),
    "replay": frozenset({"replayed"}),
    "derived": frozenset({"inferred", "projected"}),
}


class ContractValidationError(ValueError):
    """Raised when a mapping violates Canonical Event v1 semantics."""


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, name)


def _require_timestamp(value: Any, name: str) -> str:
    text = _require_text(value, name)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ContractValidationError(f"{name} must be ISO-8601 date-time") from exc
    if parsed.tzinfo is None:
        raise ContractValidationError(f"{name} must include a timezone")
    return text


def _assert_json_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractValidationError(f"{path} contains a non-string object key")
            _assert_json_value(item, f"{path}.{key}")
        return
    raise ContractValidationError(f"{path} contains a non-JSON value: {type(value).__name__}")


def _walk_secret_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _SECRET_KEYS and item != "[SECRET_REDACTED]":
                raise ContractValidationError(
                    f"secret-like field {path}.{key} must be [SECRET_REDACTED]"
                )
            _walk_secret_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_secret_fields(item, f"{path}[{index}]")
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("Bearer ") or (
            stripped.startswith("sk-") and stripped != "[SECRET_REDACTED]"
        ):
            raise ContractValidationError(f"secret-like value at {path} is not allowed")


def _walk_authority_mutations(value: Any, path: str = "$.metadata") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _AUTHORITY_MUTATION_KEYS:
                raise ContractValidationError(
                    f"Journal contract cannot carry authority mutation field {path}.{key}"
                )
            _walk_authority_mutations(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_authority_mutations(item, f"{path}[{index}]")


def _validate_source_ref(source_ref: Any) -> dict[str, Any]:
    if not isinstance(source_ref, Mapping):
        raise ContractValidationError("source_ref must be an object")
    source = dict(source_ref)
    source_kind = _require_text(source.get("source_kind"), "source_ref.source_kind")
    observation_type = _require_text(
        source.get("observation_type"), "source_ref.observation_type"
    )
    _require_text(source.get("source_id"), "source_ref.source_id")
    if source_kind not in SOURCE_KINDS:
        raise ContractValidationError(f"unsupported source_kind: {source_kind}")
    if observation_type not in OBSERVATION_TYPES:
        raise ContractValidationError(
            f"unsupported observation_type: {observation_type}"
        )
    if observation_type not in _PROVENANCE_MATRIX[source_kind]:
        raise ContractValidationError(
            f"{source_kind} cannot masquerade as observation_type={observation_type}"
        )
    _optional_text(source.get("uri"), "source_ref.uri")
    _optional_text(source.get("adapter_version"), "source_ref.adapter_version")
    return source


def _validate_attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractValidationError("attachment_ref must be an array")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ContractValidationError(f"attachment_ref[{index}] must be an object")
        item = dict(raw)
        _require_text(item.get("attachment_id"), f"attachment_ref[{index}].attachment_id")
        _require_text(item.get("media_type"), f"attachment_ref[{index}].media_type")
        _require_text(item.get("source_ref"), f"attachment_ref[{index}].source_ref")
        sha256 = item.get("sha256")
        if sha256 is not None:
            text = _require_text(sha256, f"attachment_ref[{index}].sha256")
            if len(text) != 64 or any(c not in "0123456789abcdefABCDEF" for c in text):
                raise ContractValidationError(
                    f"attachment_ref[{index}].sha256 must be 64 hex characters"
                )
        result.append(item)
    return result


def _validate_knowledge(metadata: Mapping[str, Any]) -> None:
    knowledge = metadata.get("knowledge", {})
    if not isinstance(knowledge, Mapping):
        raise ContractValidationError("metadata.knowledge must be an object")
    for key, raw in knowledge.items():
        if not isinstance(key, str) or not key:
            raise ContractValidationError("metadata.knowledge keys must be non-empty strings")
        if not isinstance(raw, Mapping):
            raise ContractValidationError(f"metadata.knowledge.{key} must be an object")
        state = raw.get("state")
        if state not in KNOWLEDGE_STATES:
            raise ContractValidationError(
                f"metadata.knowledge.{key}.state must preserve explicit knowledge semantics"
            )
        if state == "KNOWN_VALUE":
            if "value" not in raw:
                raise ContractValidationError(
                    f"metadata.knowledge.{key} KNOWN_VALUE requires value"
                )
            _assert_json_value(raw["value"], f"$.metadata.knowledge.{key}.value")
        elif "value" in raw:
            raise ContractValidationError(
                f"metadata.knowledge.{key} state={state} must not smuggle a value"
            )


def validate_canonical_event(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one canonical event and return a detached JSON-compatible copy."""

    if not isinstance(value, Mapping):
        raise ContractValidationError("canonical event must be an object")
    event = dict(value)
    missing = [field for field in _REQUIRED_FIELDS if field not in event]
    if missing:
        raise ContractValidationError(f"missing canonical event fields: {', '.join(missing)}")

    _require_text(event["event_id"], "event_id")
    _require_text(event["session_id"], "session_id")
    _require_text(event["turn_id"], "turn_id")
    sequence_no = event["sequence_no"]
    if isinstance(sequence_no, bool) or not isinstance(sequence_no, int) or sequence_no < 0:
        raise ContractValidationError("sequence_no must be a non-negative integer")
    if event["actor_role"] not in ACTOR_ROLES:
        raise ContractValidationError(f"unsupported actor_role: {event['actor_role']}")

    for field_name in (
        "message_id",
        "persona_id",
        "relationship_id",
        "provider",
        "model",
        "correction_id",
        "correction_of",
    ):
        _optional_text(event[field_name], field_name)

    _require_timestamp(event["observed_at"], "observed_at")
    _require_timestamp(event["created_at"], "created_at")
    _require_text(event["content_type"], "content_type")
    _assert_json_value(event["content_payload"], "$.content_payload")

    if event["status"] not in STATUSES:
        raise ContractValidationError(f"unsupported status: {event['status']}")
    if event["redaction_state"] not in REDACTION_STATES:
        raise ContractValidationError(
            f"unsupported redaction_state: {event['redaction_state']}"
        )

    source_ref = _validate_source_ref(event["source_ref"])
    _validate_attachments(event["attachment_ref"])

    correction_of = event["correction_of"]
    correction_id = event["correction_id"]
    if correction_of is None:
        if correction_id is not None:
            raise ContractValidationError(
                "correction_id must be null when correction_of is null"
            )
    else:
        if correction_id is None:
            raise ContractValidationError("correction events require correction_id")
        if correction_of == event["event_id"]:
            raise ContractValidationError("correction_of cannot reference the event itself")
        if source_ref["source_kind"] != "correction":
            raise ContractValidationError(
                "correction_of requires source_ref.source_kind=correction"
            )

    metadata = event["metadata"]
    if not isinstance(metadata, Mapping):
        raise ContractValidationError("metadata must be an object")
    _validate_knowledge(metadata)
    _walk_authority_mutations(metadata)
    _walk_secret_fields(event["content_payload"], "$.content_payload")
    _walk_secret_fields(metadata, "$.metadata")

    if event["redaction_state"] == "redacted":
        serialized = repr((event["content_payload"], metadata))
        if "[SECRET_REDACTED]" not in serialized:
            raise ContractValidationError(
                "redaction_state=redacted requires an explicit [SECRET_REDACTED] marker"
            )

    return deepcopy(event)


def stable_identity(value: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return provider/model-independent persona and relationship identity."""

    event = validate_canonical_event(value)
    return event["persona_id"], event["relationship_id"]


def canonical_order_key(value: Mapping[str, Any]) -> tuple[str, int, str, str, str]:
    """Deterministic replay key; provider/model are deliberately excluded."""

    event = validate_canonical_event(value)
    return (
        event["session_id"],
        event["sequence_no"],
        event["turn_id"],
        event["actor_role"],
        event["event_id"],
    )


def duplicate_event_ids(events: Iterable[Mapping[str, Any]]) -> set[str]:
    """Return stable event IDs that occur more than once."""

    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in events:
        event = validate_canonical_event(value)
        event_id = event["event_id"]
        if event_id in seen:
            duplicates.add(event_id)
        seen.add(event_id)
    return duplicates


def knowledge_state(value: Mapping[str, Any], key: str) -> str:
    """Read an explicit knowledge state without collapsing UNKNOWN semantics."""

    event = validate_canonical_event(value)
    raw = event["metadata"].get("knowledge", {}).get(key)
    if raw is None:
        raise KeyError(key)
    return raw["state"]


def authority_snapshot_after_event(
    value: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """Conformance harness: Journal evidence cannot mutate external authorities.

    The function validates the event and returns a detached, unchanged authority
    snapshot. Actual reducers/authority writers belong to later, separately
    authorized phases.
    """

    validate_canonical_event(value)
    return deepcopy(dict(snapshot))


@dataclass(frozen=True)
class CanonicalEvent:
    """Thin typed wrapper around the versioned v1 mapping."""

    data: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", validate_canonical_event(self.data))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CanonicalEvent":
        return cls(dict(value))

    def to_mapping(self) -> dict[str, Any]:
        return deepcopy(self.data)

    @property
    def order_key(self) -> tuple[str, int, str, str, str]:
        return canonical_order_key(self.data)

    @property
    def identity(self) -> tuple[str | None, str | None]:
        return stable_identity(self.data)
