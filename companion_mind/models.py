"""Public, minimal data contracts for the Phase-A runtime.

The objects stay deliberately small. They are enough to make a state change,
candidate belief, evaluation and resulting write observable without treating a
model response as truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


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

