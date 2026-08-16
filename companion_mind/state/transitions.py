"""Evidence-backed state transitions and deterministic Runtime v2 replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Sequence
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import (
    ConversationState,
    RawEvent,
    RelationshipState,
    RuntimeState,
    StableCore,
)


Confidence = Literal["low", "medium", "high"]
ConfidenceThreshold = Literal["low", "medium", "high"]

CONVERSATION_FIELDS = frozenset(
    {
        "conversation.active_topic",
        "conversation.emotional_tone",
        "conversation.open_question",
        "conversation.recent_commitments",
        "conversation.recent_shared_events",
    }
)
RELATIONSHIP_FIELDS = frozenset(
    {
        "relationship.closeness_summary",
        "relationship.recent_change",
        "relationship.last_updated_turn",
    }
)
OBSERVER_WRITE_ALLOWLIST = CONVERSATION_FIELDS | RELATIONSHIP_FIELDS

_STRING_FIELDS = frozenset(
    {
        "conversation.active_topic",
        "conversation.emotional_tone",
        "conversation.open_question",
        "relationship.closeness_summary",
        "relationship.recent_change",
    }
)
_LIST_FIELDS = frozenset(
    {
        "conversation.recent_commitments",
        "conversation.recent_shared_events",
    }
)
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TransitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StateChangeCandidate(TransitionModel):
    field: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    value: Any
    evidence_event_ids: tuple[UUID, ...] = ()
    confidence: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


class StateDeltaCandidate(TransitionModel):
    changes: tuple[StateChangeCandidate, ...] = ()


class ObserverInput(TransitionModel):
    stable_core: StableCore
    previous_conversation: ConversationState
    previous_relationship: RelationshipState
    user_event: RawEvent
    assistant_event: RawEvent

    @model_validator(mode="after")
    def validate_current_turn_pair(self) -> "ObserverInput":
        user = self.user_event
        assistant = self.assistant_event
        if user.role != "user" or assistant.role != "assistant":
            raise ValueError("observer input requires one user/assistant pair")
        if user.status != "complete" or assistant.status != "complete":
            raise ValueError("observer input requires complete RAW evidence")
        if user.session_id != assistant.session_id:
            raise ValueError("observer evidence must share one session")
        if user.turn_index != assistant.turn_index:
            raise ValueError("observer evidence must share one turn")
        if user.attempt_index != assistant.attempt_index:
            raise ValueError("observer evidence must share one attempt")
        if user.persona_id != self.stable_core.persona_id:
            raise ValueError("observer evidence must match stable_core")
        return self


class StateDeltaRecord(TransitionModel):
    schema_version: Literal["state-delta/v2"] = "state-delta/v2"
    delta_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    turn_index: int = Field(ge=1)
    field: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    old_value: Any = None
    new_value: Any = None
    evidence_event_ids: tuple[UUID, ...] = ()
    reason: str = Field(min_length=1, max_length=500)
    confidence: str = Field(min_length=1)
    accepted: bool
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_decision(self) -> "StateDeltaRecord":
        if self.accepted and self.rejection_reason is not None:
            raise ValueError("accepted delta cannot have a rejection reason")
        if not self.accepted and not self.rejection_reason:
            raise ValueError("rejected delta requires a rejection reason")
        return self


@dataclass(frozen=True)
class StateTransitionResult:
    state: RuntimeState
    records: tuple[StateDeltaRecord, ...]


class StateTransitionError(ValueError):
    """Raised when transition evidence or replay cannot be trusted."""


class DeterministicStateReducer:
    """Validate observer proposals and exclusively own Current State writes."""

    def __init__(self, *, confidence_threshold: ConfidenceThreshold = "high") -> None:
        self.confidence_threshold = confidence_threshold

    def reduce(
        self,
        state: RuntimeState,
        candidate: StateDeltaCandidate,
        *,
        user_event: RawEvent,
        assistant_event: RawEvent,
    ) -> StateTransitionResult:
        evidence = ObserverInput(
            stable_core=state.stable_core,
            previous_conversation=state.conversation,
            previous_relationship=state.relationship,
            user_event=user_event,
            assistant_event=assistant_event,
        )
        valid_event_ids = {
            evidence.user_event.event_id,
            evidence.assistant_event.event_id,
        }
        working = state
        records: list[StateDeltaRecord] = []
        seen_fields: set[str] = set()
        for change in candidate.changes:
            rejection = self._rejection_reason(
                change,
                turn_index=user_event.turn_index,
                valid_event_ids=valid_event_ids,
                seen_fields=seen_fields,
            )
            old_value = (
                self._read_field(working, change.field)
                if change.field in OBSERVER_WRITE_ALLOWLIST
                else None
            )
            if rejection is not None:
                records.append(
                    self._record(
                        state=state,
                        change=change,
                        turn_index=user_event.turn_index,
                        old_value=old_value,
                        new_value=change.value,
                        accepted=False,
                        rejection_reason=rejection,
                    )
                )
                continue

            normalized = self._normalize_value(
                change.field,
                change.value,
                turn_index=user_event.turn_index,
            )
            working = self._write_field(working, change.field, normalized)
            seen_fields.add(change.field)
            records.append(
                self._record(
                    state=state,
                    change=change,
                    turn_index=user_event.turn_index,
                    old_value=old_value,
                    new_value=normalized,
                    accepted=True,
                    rejection_reason=None,
                )
            )
        return StateTransitionResult(state=working, records=tuple(records))

    def _rejection_reason(
        self,
        change: StateChangeCandidate,
        *,
        turn_index: int,
        valid_event_ids: set[UUID],
        seen_fields: set[str],
    ) -> str | None:
        if change.field not in OBSERVER_WRITE_ALLOWLIST:
            return "field_not_allowlisted"
        if change.field in seen_fields:
            return "duplicate_field"
        if change.operation != "set":
            return "invalid_operation"
        confidence = _CONFIDENCE_RANK.get(change.confidence)
        if confidence is None:
            return "invalid_confidence"
        if confidence < _CONFIDENCE_RANK[self.confidence_threshold]:
            return "confidence_below_threshold"
        evidence_ids = set(change.evidence_event_ids)
        if not evidence_ids or not evidence_ids <= valid_event_ids:
            return "invalid_evidence"
        try:
            self._normalize_value(change.field, change.value, turn_index=turn_index)
        except StateTransitionError as exc:
            return str(exc)
        return None

    @staticmethod
    def _normalize_value(field: str, value: Any, *, turn_index: int) -> Any:
        if field in _STRING_FIELDS:
            if type(value) is not str or not value.strip():
                raise StateTransitionError("invalid_type")
            return value.strip()
        if field in _LIST_FIELDS:
            if type(value) is not list:
                raise StateTransitionError("invalid_type")
            if len(value) > 5:
                raise StateTransitionError("state_size_cap_exceeded")
            if any(type(item) is not str or not item.strip() for item in value):
                raise StateTransitionError("invalid_type")
            return [item.strip() for item in value]
        if field == "relationship.last_updated_turn":
            if type(value) is not int or value != turn_index:
                raise StateTransitionError("invalid_turn_index")
            return value
        raise StateTransitionError("field_not_allowlisted")

    @staticmethod
    def _read_field(state: RuntimeState, field: str) -> Any:
        if field.startswith("conversation."):
            return getattr(state.conversation, field.removeprefix("conversation."))
        if field.startswith("relationship."):
            return getattr(state.relationship, field.removeprefix("relationship."))
        return None

    @staticmethod
    def _write_field(state: RuntimeState, field: str, value: Any) -> RuntimeState:
        if field.startswith("conversation."):
            name = field.removeprefix("conversation.")
            conversation = state.conversation.model_copy(update={name: value})
            return state.model_copy(update={"conversation": conversation})
        name = field.removeprefix("relationship.")
        relationship = state.relationship.model_copy(update={name: value})
        return state.model_copy(update={"relationship": relationship})

    @staticmethod
    def _record(
        *,
        state: RuntimeState,
        change: StateChangeCandidate,
        turn_index: int,
        old_value: Any,
        new_value: Any,
        accepted: bool,
        rejection_reason: str | None,
    ) -> StateDeltaRecord:
        return StateDeltaRecord(
            session_id=state.session.session_id,
            turn_index=turn_index,
            field=change.field,
            operation=change.operation,
            old_value=old_value,
            new_value=new_value,
            evidence_event_ids=change.evidence_event_ids,
            reason=change.reason,
            confidence=change.confidence,
            accepted=accepted,
            rejection_reason=rejection_reason,
        )


def replay_runtime_state(
    initial_state: RuntimeState,
    raw_events: Sequence[RawEvent],
    delta_records: Sequence[StateDeltaRecord],
) -> RuntimeState:
    """Reconstruct Runtime State exactly from S0, RAW, and accepted deltas."""

    working = initial_state
    event_by_id = {event.event_id: event for event in raw_events}
    if len(event_by_id) != len(raw_events):
        raise StateTransitionError("duplicate RAW event_id")

    for record in delta_records:
        if not record.accepted:
            continue
        if record.session_id != initial_state.session.session_id:
            raise StateTransitionError("delta belongs to a foreign session")
        evidence = [event_by_id.get(event_id) for event_id in record.evidence_event_ids]
        if not evidence or any(event is None for event in evidence):
            raise StateTransitionError("delta evidence is missing from RAW")
        if any(event.turn_index != record.turn_index for event in evidence if event):
            raise StateTransitionError("delta evidence is from the wrong turn")
        old_value = DeterministicStateReducer._read_field(working, record.field)
        if old_value != record.old_value:
            raise StateTransitionError("delta old_value does not match replay state")
        normalized = DeterministicStateReducer._normalize_value(
            record.field,
            record.new_value,
            turn_index=record.turn_index,
        )
        working = DeterministicStateReducer._write_field(
            working,
            record.field,
            normalized,
        )

    successful = _successful_assistant_events(raw_events)
    if not successful:
        return working
    last = successful[-1]
    previous_provider = (
        successful[-2].provider
        if len(successful) > 1
        else initial_state.session.active_provider
    )
    session = working.session.model_copy(
        update={
            "active_provider": last.provider,
            "last_provider": previous_provider,
            "turn_index": last.turn_index,
        }
    )
    return working.model_copy(
        update={"session": session, "updated_at": last.created_at}
    )


def _successful_assistant_events(
    raw_events: Sequence[RawEvent],
) -> tuple[RawEvent, ...]:
    users = {
        (event.turn_index, event.attempt_index)
        for event in raw_events
        if event.role == "user" and event.status == "complete"
    }
    assistants: dict[int, list[RawEvent]] = {}
    for event in raw_events:
        attempt = (event.turn_index, event.attempt_index)
        if (
            event.role == "assistant"
            and event.status == "complete"
            and attempt in users
        ):
            assistants.setdefault(event.turn_index, []).append(event)
    selected = [
        max(events, key=lambda event: event.attempt_index)
        for _, events in sorted(assistants.items())
    ]
    return tuple(selected)
