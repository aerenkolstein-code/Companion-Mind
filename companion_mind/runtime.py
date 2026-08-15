"""Deterministic stateful runtime with durable events and replay."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from itertools import count
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    AgendaItem,
    BeliefCandidate,
    CognitiveTask,
    DecisionTrace,
    Evaluation,
    Event,
    StateDelta,
)


class EventLogError(ValueError):
    """Raised when a durable event log cannot be trusted for replay."""


class JsonlEventStore:
    """Append-only, fsynced event store using a public JSONL contract."""

    schema_version = "companion-mind-event/v1"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"schema_version": self.schema_version, "event": event.to_dict()}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> list[Event]:
        if not self.path.exists():
            return []

        events: list[Event] = []
        seen: set[str] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EventLogError(
                        f"invalid JSON at {self.path}:{line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise EventLogError(f"record {line_number} must be an object")
                if record.get("schema_version") != self.schema_version:
                    raise EventLogError(
                        f"unsupported schema at {self.path}:{line_number}"
                    )
                raw_event = record.get("event")
                if not isinstance(raw_event, dict):
                    raise EventLogError(
                        f"record {line_number} does not contain an event object"
                    )
                try:
                    event = Event.from_mapping(raw_event)
                except ValueError as exc:
                    raise EventLogError(
                        f"invalid event at {self.path}:{line_number}: {exc}"
                    ) from exc
                if event.event_id in seen:
                    raise EventLogError(
                        f"duplicate event_id {event.event_id!r} at line {line_number}"
                    )
                seen.add(event.event_id)
                events.append(event)
        return events


class ClosureGuard:
    """Reject premature parent closure while required child work remains."""

    safeguard_id = "CM-GUARD-001"
    terminal_statuses = frozenset({"DONE", "CANCELLED"})
    blocking_statuses = frozenset(
        {
            "OPEN",
            "UNKNOWN",
            "WAITING",
            "WAITING-ON-TRIGGER",
            "WAITING-EXTERNAL",
            "BLOCKED",
            "PENDING",
        }
    )

    def evaluate(self, children: Iterable[Mapping[str, str]]) -> Evaluation:
        normalized = [
            {
                "child_id": str(child.get("child_id", "unnamed")),
                "status": str(child.get("status", "UNKNOWN")).strip().upper(),
            }
            for child in children
        ]
        if not normalized:
            return Evaluation(
                "HOLD",
                "No child-state evidence was supplied; closure cannot be verified.",
                0.0,
                {"child_state_present": False, "all_required_children_terminal": False},
                self.safeguard_id,
            )

        blockers = [
            child
            for child in normalized
            if child["status"] in self.blocking_statuses
            or child["status"] not in self.terminal_statuses
        ]
        if blockers:
            blocker_text = ", ".join(
                f"{child['child_id']}={child['status']}" for child in blockers
            )
            return Evaluation(
                "REJECT",
                f"Parent closure blocked by required child state: {blocker_text}.",
                1.0,
                {"child_state_present": True, "all_required_children_terminal": False},
                self.safeguard_id,
            )

        return Evaluation(
            "ACCEPT",
            "All required child tasks are terminal.",
            1.0,
            {"child_state_present": True, "all_required_children_terminal": True},
            self.safeguard_id,
        )


class CompanionRuntime:
    """Observable event-to-state loop backed by a replayable event log."""

    terminal_statuses = ClosureGuard.terminal_statuses
    supported_event_kinds = frozenset(
        {"agenda_item_upserted", "parent_closure_requested"}
    )

    def __init__(
        self,
        *,
        max_tasks_per_event: int = 1,
        event_store: JsonlEventStore | None = None,
    ) -> None:
        if max_tasks_per_event < 1:
            raise ValueError("max_tasks_per_event must be positive")
        self.state: dict[str, Any] = {}
        self.agenda: dict[str, AgendaItem] = {}
        self.deltas: list[StateDelta] = []
        self.traces: list[DecisionTrace] = []
        self._processed_events: set[str] = set()
        self._processed_tasks: set[str] = set()
        self._ids = count(1)
        self._max_tasks_per_event = max_tasks_per_event
        self.event_store = event_store
        self.closure_guard = ClosureGuard()

    @classmethod
    def replay(
        cls,
        event_store: JsonlEventStore,
        *,
        max_tasks_per_event: int = 1,
    ) -> "CompanionRuntime":
        runtime = cls(
            max_tasks_per_event=max_tasks_per_event,
            event_store=event_store,
        )
        for event in event_store.read():
            runtime.ingest(event, persist=False)
        return runtime

    def ingest(self, event: Event, *, persist: bool = True) -> list[DecisionTrace]:
        """Persist and process one event exactly once."""

        if event.event_id in self._processed_events:
            return []
        self._validate_event(event)
        if persist and self.event_store is not None:
            self.event_store.append(event)
        self._processed_events.add(event.event_id)

        event_deltas = self._apply_event(event)
        self.deltas.extend(event_deltas)
        tasks = self._discover_tasks(event, event_deltas)[: self._max_tasks_per_event]
        traces: list[DecisionTrace] = []

        for task in tasks:
            if task.dedupe_key in self._processed_tasks:
                continue
            self._processed_tasks.add(task.dedupe_key)
            candidate = self._reason(task, event)
            evaluation = self._evaluate(task, candidate, event)
            writes = self._commit(candidate, evaluation, event)
            self.deltas.extend(writes)
            trace = DecisionTrace(
                event_id=event.event_id,
                task_id=task.task_id,
                belief_id=candidate.belief_id,
                evaluation=evaluation,
                state_writes=tuple(writes),
            )
            self.traces.append(trace)
            traces.append(trace)

        return traces

    @staticmethod
    def _validate_event(event: Event) -> None:
        if event.kind not in CompanionRuntime.supported_event_kinds:
            raise ValueError(f"unsupported event kind: {event.kind}")
        if event.kind == "agenda_item_upserted":
            if not str(event.payload.get("item_id", "")).strip():
                raise ValueError("agenda_item_upserted requires item_id and parent_id")
            if not str(event.payload.get("parent_id", "")).strip():
                raise ValueError("agenda_item_upserted requires item_id and parent_id")
            if not isinstance(event.payload.get("required", True), bool):
                raise ValueError("agenda item required must be boolean")
            return

        children = event.payload.get("children")
        if children is not None and (
            not isinstance(children, list)
            or any(not isinstance(child, Mapping) for child in children)
        ):
            raise ValueError("children must be a list of objects")

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "agenda": {
                item_id: asdict(item)
                for item_id, item in sorted(self.agenda.items())
            },
            "deltas": [asdict(delta) for delta in self.deltas],
            "traces": [asdict(trace) for trace in self.traces],
            "processed_event_ids": sorted(self._processed_events),
        }

    def _apply_event(self, event: Event) -> list[StateDelta]:
        deltas: list[StateDelta] = []
        if event.kind == "agenda_item_upserted":
            deltas.extend(self._apply_agenda_item(event))

        parent_id = str(event.payload.get("parent_id", "parent-goal"))
        parent_statuses = dict(self.state.get("parent_status", {}))
        if parent_id not in parent_statuses:
            parent_statuses[parent_id] = "OPEN"
            self.state["parent_status"] = parent_statuses
            deltas.append(
                StateDelta(
                    key=f"parent_status.{parent_id}",
                    old_value=None,
                    new_value="OPEN",
                    source_event_id=event.event_id,
                    cause="parent_discovered",
                )
            )

        previous = self.state.get("last_event_id")
        self.state["last_event_id"] = event.event_id
        deltas.append(
            StateDelta(
                key="last_event_id",
                old_value=previous,
                new_value=event.event_id,
                source_event_id=event.event_id,
                cause="event_ingest",
            )
        )
        return deltas

    def _apply_agenda_item(self, event: Event) -> list[StateDelta]:
        item_id = str(event.payload.get("item_id", "")).strip()
        parent_id = str(event.payload.get("parent_id", "")).strip()
        if not item_id or not parent_id:
            raise ValueError("agenda_item_upserted requires item_id and parent_id")
        status = str(event.payload.get("status", "UNKNOWN")).strip().upper()
        status = status or "UNKNOWN"
        required = event.payload.get("required", True)
        if not isinstance(required, bool):
            raise ValueError("agenda item required must be boolean")

        item = AgendaItem(
            item_id=item_id,
            parent_id=parent_id,
            status=status,
            required=required,
            source_event_id=event.event_id,
        )
        child_states = dict(self.state.get("child_state", {}))
        previous = child_states.get(item_id)
        child_states[item_id] = asdict(item)
        self.state["child_state"] = child_states

        if status in self.terminal_statuses:
            self.agenda.pop(item_id, None)
        else:
            self.agenda[item_id] = item

        return [
            StateDelta(
                key=f"child_state.{item_id}",
                old_value=previous,
                new_value=asdict(item),
                source_event_id=event.event_id,
                cause="agenda_item_upserted",
            )
        ]

    def _discover_tasks(
        self, event: Event, deltas: list[StateDelta]
    ) -> list[CognitiveTask]:
        if event.kind != "parent_closure_requested" or not deltas:
            return []
        parent_id = str(event.payload.get("parent_id", "parent-goal"))
        return [
            CognitiveTask(
                task_id=f"task-{next(self._ids):03d}",
                task_type="closure_check",
                question=f"May {parent_id} be marked DONE?",
                trigger_event_id=event.event_id,
                context_refs=(event.event_id, f"state:parent_status:{parent_id}"),
                dedupe_key=f"closure:{parent_id}:{event.event_id}",
            )
        ]

    def _reason(self, task: CognitiveTask, event: Event) -> BeliefCandidate:
        parent_id = str(event.payload.get("parent_id", "parent-goal"))
        return BeliefCandidate(
            belief_id=f"belief-{next(self._ids):03d}",
            claim=f"{parent_id} may be marked DONE.",
            confidence=0.90,
            provenance=(event.event_id, event.source, *task.context_refs),
        )

    def _evaluate(
        self, task: CognitiveTask, candidate: BeliefCandidate, event: Event
    ) -> Evaluation:
        if not candidate.provenance:
            return Evaluation(
                "HOLD",
                "Candidate has no provenance.",
                0.0,
                {"source_present": False},
            )
        if task.task_type == "closure_check":
            children = event.payload.get("children")
            if children is None:
                children = self._children_from_state(
                    str(event.payload.get("parent_id", "parent-goal"))
                )
            if not isinstance(children, list):
                return Evaluation(
                    "HOLD",
                    "Child-state evidence must be a list.",
                    0.0,
                    {"child_state_valid": False},
                    self.closure_guard.safeguard_id,
                )
            return self.closure_guard.evaluate(children)
        return Evaluation(
            "HOLD",
            "No evaluator is registered for this task type.",
            0.0,
            {"evaluator_registered": False},
        )

    def _children_from_state(self, parent_id: str) -> list[dict[str, str]]:
        children = []
        for item_id, raw_item in sorted(self.state.get("child_state", {}).items()):
            if raw_item.get("parent_id") != parent_id or not raw_item.get("required"):
                continue
            children.append(
                {"child_id": item_id, "status": str(raw_item.get("status", "UNKNOWN"))}
            )
        return children

    def _commit(
        self, candidate: BeliefCandidate, evaluation: Evaluation, event: Event
    ) -> list[StateDelta]:
        if evaluation.decision != "ACCEPT":
            return []
        parent_id = str(event.payload.get("parent_id", "parent-goal"))
        statuses = dict(self.state.get("parent_status", {}))
        old = statuses.get(parent_id, "OPEN")
        statuses[parent_id] = "DONE"
        self.state["parent_status"] = statuses
        return [
            StateDelta(
                key=f"parent_status.{parent_id}",
                old_value=old,
                new_value="DONE",
                source_event_id=event.event_id,
                cause=f"accepted:{candidate.belief_id}",
            )
        ]


def _demo_events() -> tuple[Event, ...]:
    source = "synthetic-public-demo"
    return (
        Event(
            "evt-demo-001",
            "agenda_item_upserted",
            {
                "item_id": "quick-check",
                "parent_id": "onboarding",
                "status": "DONE",
                "required": True,
            },
            source,
        ),
        Event(
            "evt-demo-002",
            "agenda_item_upserted",
            {
                "item_id": "qualification",
                "parent_id": "onboarding",
                "status": "OPEN",
                "required": True,
            },
            source,
        ),
        Event(
            "evt-demo-003",
            "parent_closure_requested",
            {"parent_id": "onboarding"},
            source,
        ),
        Event(
            "evt-demo-004",
            "agenda_item_upserted",
            {
                "item_id": "qualification",
                "parent_id": "onboarding",
                "status": "DONE",
                "required": True,
            },
            source,
        ),
        Event(
            "evt-demo-005",
            "parent_closure_requested",
            {"parent_id": "onboarding"},
            source,
        ),
    )


def demo(event_store: JsonlEventStore | None = None) -> dict[str, Any]:
    runtime = (
        CompanionRuntime.replay(event_store)
        if event_store is not None
        else CompanionRuntime()
    )
    for event in _demo_events():
        runtime.ingest(event)
    return runtime.snapshot()


def _load_event(path: str) -> Event:
    if path == "-":
        raw = json.load(sys.stdin)
    else:
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("event JSON must be an object")
    return Event.from_mapping(raw)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="companion-mind",
        description="Persist events, run guarded state transitions, and replay state.",
    )
    subparsers = parser.add_subparsers(dest="command")

    demo_parser = subparsers.add_parser("demo", help="run the public stateful demo")
    demo_parser.add_argument("--event-log", type=Path)

    replay_parser = subparsers.add_parser("replay", help="rebuild state from JSONL")
    replay_parser.add_argument("--event-log", type=Path, required=True)

    ingest_parser = subparsers.add_parser("ingest", help="ingest one event JSON")
    ingest_parser.add_argument("--event-log", type=Path, required=True)
    ingest_parser.add_argument("--event", required=True, help="JSON path or - for stdin")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in (None, "demo"):
            path = getattr(args, "event_log", None)
            snapshot = demo(JsonlEventStore(path) if path else None)
        elif args.command == "replay":
            snapshot = CompanionRuntime.replay(JsonlEventStore(args.event_log)).snapshot()
        else:
            store = JsonlEventStore(args.event_log)
            runtime = CompanionRuntime.replay(store)
            runtime.ingest(_load_event(args.event))
            snapshot = runtime.snapshot()
    except (EventLogError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
