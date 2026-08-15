"""Public data contracts for the stateful Companion-Mind runtime.

The objects stay deliberately small and serializable. They make evidence,
agenda state, candidate beliefs, evaluations, and resulting writes observable
without treating a model response as truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


Decision = Literal["ACCEPT", "REJECT", "HOLD"]


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: str
    payload: dict[str, Any]
    source: str

    def __post_init__(self) -> None:
        if not self.event_id or not self.kind or not self.source:
            raise ValueError("event_id, kind and source are required")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")
        object.__setattr__(self, "payload", dict(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "payload": self.payload,
            "source": self.source,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Event":
        try:
            payload = value["payload"]
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            required_text = {}
            for field_name in ("event_id", "kind", "source"):
                field_value = value[field_name]
                if not isinstance(field_value, str) or not field_value.strip():
                    raise ValueError(f"{field_name} must be a non-empty string")
                required_text[field_name] = field_value
            return cls(
                event_id=required_text["event_id"],
                kind=required_text["kind"],
                payload=payload,
                source=required_text["source"],
            )
        except KeyError as exc:
            raise ValueError(f"missing event field: {exc.args[0]}") from exc


@dataclass(frozen=True)
class AgendaItem:
    item_id: str
    parent_id: str
    status: str
    required: bool
    source_event_id: str

    def __post_init__(self) -> None:
        if not self.item_id or not self.parent_id or not self.source_event_id:
            raise ValueError("item_id, parent_id and source_event_id are required")


@dataclass(frozen=True)
class StateDelta:
    key: str
    old_value: Any
    new_value: Any
    source_event_id: str
    cause: str


@dataclass(frozen=True)
class CognitiveTask:
    task_id: str
    task_type: str
    question: str
    trigger_event_id: str
    context_refs: tuple[str, ...]
    dedupe_key: str


@dataclass(frozen=True)
class BeliefCandidate:
    belief_id: str
    claim: str
    confidence: float
    provenance: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class Evaluation:
    decision: Decision
    reason: str
    score: float
    checks: dict[str, bool] = field(default_factory=dict)
    safeguard_id: str | None = None


@dataclass(frozen=True)
class DecisionTrace:
    event_id: str
    task_id: str
    belief_id: str
    evaluation: Evaluation
    state_writes: tuple[StateDelta, ...] = field(default_factory=tuple)
