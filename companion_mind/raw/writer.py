"""Append-only Unified RAW persistence for persona-runtime conversations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable
from uuid import UUID

from pydantic import ValidationError

from companion_mind.state import RawEvent


class RawStoreError(ValueError):
    """Raised when Unified RAW cannot be written or trusted."""


class UnifiedRawWriter:
    """Append validated events to one immutable JSONL timeline per session."""

    def __init__(self, raw_dir: str | Path) -> None:
        self.raw_dir = Path(raw_dir)

    def path_for(self, session_id: UUID | str) -> Path:
        try:
            normalized = UUID(str(session_id))
        except ValueError as exc:
            raise RawStoreError("session_id must be a UUID") from exc
        return self.raw_dir / f"{normalized}.jsonl"

    def append(self, event: RawEvent) -> Path:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(event.session_id)
        payload = event.model_dump_json() + "\n"
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise RawStoreError("cannot append Unified RAW event") from exc
        return path

    def read(self, session_id: UUID | str) -> tuple[RawEvent, ...]:
        path = self.path_for(session_id)
        if not path.exists():
            return ()
        events: list[RawEvent] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        raise RawStoreError(
                            f"blank Unified RAW record at line {line_number}"
                        )
                    try:
                        events.append(RawEvent.model_validate_json(line))
                    except ValidationError as exc:
                        raise RawStoreError(
                            f"invalid Unified RAW record at line {line_number}"
                        ) from exc
        except OSError as exc:
            raise RawStoreError("cannot read Unified RAW") from exc
        self._validate_timeline(events, session_id)
        return tuple(events)

    @staticmethod
    def _validate_timeline(events: Iterable[RawEvent], session_id: UUID | str) -> None:
        expected_session = UUID(str(session_id))
        seen_event_ids: set[UUID] = set()
        last_turn = -1
        persona_id: str | None = None
        universe: str | None = None
        for event in events:
            if event.session_id != expected_session:
                raise RawStoreError("Unified RAW contains a foreign session")
            if event.event_id in seen_event_ids:
                raise RawStoreError("Unified RAW contains a duplicate event_id")
            if event.turn_index < last_turn:
                raise RawStoreError("Unified RAW turn order moved backwards")
            if persona_id is None:
                persona_id = event.persona_id
                universe = event.universe
            if event.persona_id != persona_id or event.universe != universe:
                raise RawStoreError("Unified RAW identity changed inside one session")
            seen_event_ids.add(event.event_id)
            last_turn = event.turn_index
