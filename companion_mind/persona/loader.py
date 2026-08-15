"""Load canonical persona YAML without allowing provider coupling."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from .models import PersonaState


class PersonaLoadError(ValueError):
    """Raised when a canonical persona file is missing or invalid."""


_SAFE_PERSONA_ID = re.compile(r"^[A-Z][A-Z0-9-]*$")
_FORBIDDEN_PROVIDER_TERMS = frozenset(
    {"provider", "model", "deepseek", "grok", "xai"}
)


def _contains_provider_coupling(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).strip().lower() in _FORBIDDEN_PROVIDER_TERMS
            or _contains_provider_coupling(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_provider_coupling(item) for item in value)
    if isinstance(value, str):
        tokens = set(re.findall(r"[a-z0-9_-]+", value.lower()))
        return bool(tokens & _FORBIDDEN_PROVIDER_TERMS)
    return False


class PersonaLoader:
    """Resolve and validate one provider-independent persona document."""

    def __init__(self, personas_dir: str | Path) -> None:
        self.personas_dir = Path(personas_dir)

    def load(self, persona_id: str) -> PersonaState:
        normalized = persona_id.strip().upper()
        if not _SAFE_PERSONA_ID.fullmatch(normalized):
            raise PersonaLoadError("persona_id contains unsupported characters")

        filename = normalized.lower().replace("-", "_") + ".yaml"
        path = self.personas_dir / filename
        if not path.is_file():
            raise PersonaLoadError(f"unknown persona_id: {normalized}")

        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PersonaLoadError(f"cannot read persona {normalized}: {exc}") from exc
        if not isinstance(document, Mapping):
            raise PersonaLoadError("persona document must be an object")
        if _contains_provider_coupling(document):
            raise PersonaLoadError("persona document must not contain provider information")

        try:
            persona = PersonaState.model_validate(document)
        except ValidationError as exc:
            raise PersonaLoadError(f"invalid persona {normalized}: {exc}") from exc
        if persona.persona_id != normalized:
            raise PersonaLoadError(
                f"persona_id mismatch: requested {normalized}, found {persona.persona_id}"
            )
        return persona
