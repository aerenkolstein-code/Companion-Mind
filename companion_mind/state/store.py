"""Atomic local persistence for current runtime state."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from .models import RuntimeState


class StateStoreError(ValueError):
    """Raised when persisted runtime state cannot be trusted."""


class JsonStateStore:
    """Persist one validated JSON snapshot per session using atomic replace."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)

    def path_for(self, session_id: UUID | str) -> Path:
        try:
            normalized = UUID(str(session_id))
        except ValueError as exc:
            raise StateStoreError("session_id must be a UUID") from exc
        return self.state_dir / f"{normalized}.json"

    def save(self, state: RuntimeState) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        target = self.path_for(state.session.session_id)
        temporary = target.with_suffix(".json.tmp")
        payload = state.model_dump_json(indent=2) + "\n"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise StateStoreError(f"cannot save runtime state: {exc}") from exc
        return target

    def load(self, session_id: UUID | str) -> RuntimeState:
        path = self.path_for(session_id)
        try:
            payload = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StateStoreError(f"cannot read runtime state: {exc}") from exc
        try:
            return RuntimeState.model_validate_json(payload)
        except ValidationError as exc:
            raise StateStoreError(f"invalid runtime state: {exc}") from exc
