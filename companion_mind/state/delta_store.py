"""Append-only persistence for accepted and rejected Runtime v2 deltas."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable
from uuid import UUID

from pydantic import ValidationError

from .transitions import StateDeltaRecord


class DeltaStoreError(ValueError):
    """Raised when a delta journal cannot be written or trusted."""


class JsonlDeltaStore:
    def __init__(self, delta_dir: str | Path) -> None:
        self.delta_dir = Path(delta_dir)

    def path_for(self, session_id: UUID | str) -> Path:
        try:
            normalized = UUID(str(session_id))
        except ValueError as exc:
            raise DeltaStoreError("session_id must be a UUID") from exc
        return self.delta_dir / f"{normalized}.jsonl"

    def append_many(self, records: Iterable[StateDeltaRecord]) -> Path | None:
        materialized = tuple(records)
        if not materialized:
            return None
        session_ids = {record.session_id for record in materialized}
        if len(session_ids) != 1:
            raise DeltaStoreError("one append cannot mix sessions")
        session_id = next(iter(session_ids))
        self.delta_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(session_id)
        payload = "".join(record.model_dump_json() + "\n" for record in materialized)
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise DeltaStoreError("cannot append state delta") from exc
        return path

    def read(self, session_id: UUID | str) -> tuple[StateDeltaRecord, ...]:
        path = self.path_for(session_id)
        if not path.exists():
            return ()
        records: list[StateDeltaRecord] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        raise DeltaStoreError(
                            f"blank delta record at line {line_number}"
                        )
                    try:
                        records.append(StateDeltaRecord.model_validate_json(line))
                    except ValidationError as exc:
                        raise DeltaStoreError(
                            f"invalid delta record at line {line_number}"
                        ) from exc
        except OSError as exc:
            raise DeltaStoreError("cannot read state delta") from exc
        self._validate(records, UUID(str(session_id)))
        return tuple(records)

    @staticmethod
    def _validate(records: Iterable[StateDeltaRecord], session_id: UUID) -> None:
        seen: set[UUID] = set()
        last_turn = 0
        for record in records:
            if record.session_id != session_id:
                raise DeltaStoreError("delta journal contains a foreign session")
            if record.delta_id in seen:
                raise DeltaStoreError("delta journal contains a duplicate delta_id")
            if record.turn_index < last_turn:
                raise DeltaStoreError("delta journal turn order moved backwards")
            seen.add(record.delta_id)
            last_turn = record.turn_index
