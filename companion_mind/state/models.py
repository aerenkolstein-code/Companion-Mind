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


class FrozenRuntimeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class RelationshipCore(FrozenRuntimeModel):
    counterpart_id: str = Field(min_length=1)
    counterpart: str = Field(min_length=1)
    relationship_class: Literal["established_romantic_relationship"]
    status: Literal["current"]


class StableCore(FrozenRuntimeModel):
    """Small immutable identity and relationship boundary for Runtime v2."""

    persona_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    nickname: str = Field(min_length=1)
    universe: str = Field(min_length=1)
    role: str = Field(min_length=1)
    core_traits: tuple[str, ...] = Field(min_length=1)
    primary_language: str = Field(min_length=1)
    voice_style: tuple[str, ...] = Field(min_length=1)
    biography: tuple[str, ...] | None = None
    relationship_core: RelationshipCore

    @classmethod
    def from_persona(cls, persona: PersonaState) -> "StableCore":
        relationship = persona.relationship
        return cls(
            persona_id=persona.persona_id,
            display_name=persona.display_name,
            nickname=persona.nickname,
            universe=persona.universe,
            role=persona.identity.role,
            core_traits=persona.core_traits,
            primary_language=persona.voice.primary_language,
            voice_style=persona.voice.style,
            relationship_core=RelationshipCore(
                counterpart_id=relationship.counterpart_id,
                counterpart=relationship.counterpart,
                relationship_class=relationship.relationship_class,
                status=relationship.status,
            ),
        )


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
    closeness_summary: str | None = Field(default=None, min_length=1)
    recent_change: str | None = Field(default=None, min_length=1)
    last_updated_turn: int | None = Field(default=None, ge=0)


class ConversationState(RuntimeModel):
    active_topic: str | None = Field(default=None, min_length=1)
    emotional_tone: str | None = Field(default=None, min_length=1)
    open_question: str | None = Field(default=None, min_length=1)
    recent_commitments: list[str] = Field(default_factory=list, max_length=5)
    recent_shared_events: list[str] = Field(default_factory=list, max_length=5)


class RawEvent(RuntimeModel):
    """Unified RAW contract; persistence is added in a later step."""

    event_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    turn_index: int = Field(ge=0)
    persona_id: str = Field(min_length=1)
    universe: str = Field(min_length=1)
    role: RawRole
    attempt_index: int = Field(default=1, ge=1)
    provider: str | None = None
    model: str | None = None
    route_state: RouteState = "NORMAL"
    route_reason: str = "runtime_default"
    content: str
    created_at: datetime = Field(default_factory=utc_now)
    status: Literal["complete", "failed", "correction"] = "complete"

    @model_validator(mode="after")
    def validate_provenance(self) -> "RawEvent":
        has_provider = self.provider is not None
        has_model = self.model is not None
        if has_provider != has_model:
            raise ValueError("provider and model must be recorded together")
        if self.role == "assistant" and not has_provider:
            raise ValueError("assistant RAW events require provider and model")
        if self.role == "user" and has_provider:
            raise ValueError("user RAW events must not claim provider provenance")
        if self.status == "failed" and self.role != "runtime":
            raise ValueError("failed RAW events must use the runtime role")
        return self


class RuntimeState(RuntimeModel):
    schema_version: Literal["lin-zhiyao-runtime-state/v2"] = (
        "lin-zhiyao-runtime-state/v2"
    )
    persona: PersonaState
    stable_core: StableCore
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
        expected_core = StableCore.from_persona(self.persona)
        if self.stable_core != expected_core:
            raise ValueError("stable_core must match canonical persona")
        relationship_core = self.stable_core.relationship_core
        if self.relationship.counterpart_id != relationship_core.counterpart_id:
            raise ValueError("relationship counterpart must match stable_core")
        if self.relationship.relationship_status != relationship_core.status:
            raise ValueError("relationship status must match stable_core")
        return self
