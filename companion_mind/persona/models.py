"""Validated, provider-independent persona contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    """Strict immutable base for canonical persona data."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PersonaIdentity(FrozenModel):
    role: str = Field(min_length=1)
    continuity_owner: str = Field(pattern="^runtime$")


class PersonaRelationship(FrozenModel):
    counterpart_id: str = Field(min_length=1)
    counterpart: str = Field(min_length=1)
    relationship_class: str = Field(
        pattern="^established_romantic_relationship$"
    )
    status: str = Field(pattern="^current$")
    closeness: str = Field(pattern="^runtime_managed$")


class PersonaVoice(FrozenModel):
    primary_language: str = Field(min_length=1)
    style: tuple[str, ...] = Field(min_length=1)


class PersonaState(FrozenModel):
    """Minimal canonical identity held by the runtime, never by a provider."""

    schema_version: str = Field(pattern=r"^persona/v1$")
    persona_id: str = Field(pattern=r"^[A-Z][A-Z0-9-]*$")
    display_name: str = Field(min_length=1)
    nickname: str = Field(min_length=1)
    universe: str = Field(min_length=1)
    identity: PersonaIdentity
    core_traits: tuple[str, ...] = Field(min_length=1)
    relationship: PersonaRelationship
    voice: PersonaVoice
    hard_constraints: tuple[str, ...] = Field(min_length=1)
