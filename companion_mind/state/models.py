"""Validated state contracts for the LIN-ZHIYAO runtime experiment."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from companion_mind.persona.models import PersonaState


RouteState = Literal["NORMAL", "ROMANTIC", "ADULT_BOUNDARY"]
RawRole = Literal["user", "assistant", "runtime"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SessionState(RuntimeModel):
    session_id: UUID = Field(default_factory=uuid4)
    persona_id: str = Field(min_length=1)
    universe: str = Field(min_length=1)
    active_provider: str | None = None
    active_route: RouteState = "NORMAL"
    turn_index: int = Field(default=0, ge=0)
    last_provider: str | None = None


class RelationshipState(RuntimeModel):
    persona_id: str = Field(min_length=1)
    counterpart_id: str = Field(min_length=1)
    relationship_status: str = Field(pattern="^current$")
    closeness_summary: str = ""
    recent_change: str | None = None
    last_updated_turn: int = Field(default=0, ge=0)


class ConversationState(RuntimeModel):
    active_topic: str = ""
    emotional_tone: str = ""
    open_question: str | None = None
    recent_commitments: list[str] = Field(default_factory=list)
    recent_shared_events: list[str] = Field(default_factory=list)


class RawEvent(RuntimeModel):
    """Unified RAW contract; persistence is added in a later step."""

    event_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    turn_index: int = Field(ge=0)
    persona_id: str = Field(min_length=1)
    universe: str = Field(min_length=1)
    role: RawRole
    provider: str | None = None
    model: str | None = None
    route_state: RouteState = "NORMAL"
    route_reason: str = "runtime_default"
    content: str
    created_at: datetime = Field(default_factory=utc_now)
    status: Literal["complete", "failed", "correction"] = "complete"


class RuntimeState(RuntimeModel):
    schema_version: Literal["lin-zhiyao-runtime-state/v1"] = (
        "lin-zhiyao-runtime-state/v1"
    )
    persona: PersonaState
    session: SessionState
    relationship: RelationshipState
    conversation: ConversationState
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_identity_continuity(self) -> "RuntimeState":
        persona_id = self.persona.persona_id
        if self.session.persona_id != persona_id:
            raise ValueError("session persona_id must match canonical persona")
        if self.relationship.persona_id != persona_id:
            raise ValueError("relationship persona_id must match canonical persona")
        if self.session.universe != self.persona.universe:
            raise ValueError("session universe must match canonical persona")
        return self
