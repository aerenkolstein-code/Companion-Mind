"""Deterministic stateful runtime with durable events and replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
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


class MitigationSpecError(ValueError):
    """Raised when an executable mitigation contract is invalid or unsupported."""


def _required_text(value: Mapping[str, Any], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not field.strip():
        raise MitigationSpecError(f"{key} must be a non-empty string")
    return field.strip()


def _required_statuses(value: Mapping[str, Any], key: str) -> frozenset[str]:
    raw = value.get(key)
    if not isinstance(raw, list) or not raw:
        raise MitigationSpecError(f"runtime.{key} must be a non-empty array")
    statuses: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise MitigationSpecError(
                f"runtime.{key} must contain non-empty strings"
            )
        statuses.add(item.strip().upper())
    return frozenset(statuses)


@dataclass(frozen=True)
class MitigationSpec:
    """Validated, canonical configuration for one runtime safeguard."""

    schema_version: str
    mitigation_id: str
    target_failure: str
    guard_type: str
    safeguard_id: str
    terminal_statuses: frozenset[str]
    blocking_statuses: frozenset[str]
    empty_evidence_decision: str
    non_terminal_decision: str
    all_terminal_decision: str

    @classmethod
    def default(cls) -> "MitigationSpec":
        return cls(
            schema_version="mitigation-spec/v1",
            mitigation_id="MIT-CLOSURE-GUARD-001",
            target_failure="premature_parent_closure",
            guard_type="closure_guard",
            safeguard_id="CM-GUARD-001",
            terminal_statuses=frozenset({"DONE", "CANCELLED"}),
            blocking_statuses=frozenset(
                {
                    "OPEN",
                    "UNKNOWN",
                    "WAITING",
                    "WAITING-ON-TRIGGER",
                    "WAITING-EXTERNAL",
                    "BLOCKED",
                    "PENDING",
                }
            ),
            empty_evidence_decision="HOLD",
            non_terminal_decision="REJECT",
            all_terminal_decision="ACCEPT",
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MitigationSpec":
        if not isinstance(value, Mapping):
            raise MitigationSpecError("mitigation spec must be an object")
        schema_version = _required_text(value, "schema_version")
        if schema_version != "mitigation-spec/v1":
            raise MitigationSpecError(
                f"unsupported schema_version: {schema_version!r}"
            )
        runtime = value.get("runtime")
        if not isinstance(runtime, Mapping):
            raise MitigationSpecError("runtime must be an object")

        guard_type = _required_text(runtime, "guard_type")
        if guard_type != "closure_guard":
            raise MitigationSpecError(f"unsupported guard_type: {guard_type!r}")

        decisions = {
            "empty_evidence_decision": _required_text(
                runtime, "empty_evidence_decision"
            ).upper(),
            "non_terminal_decision": _required_text(
                runtime, "non_terminal_decision"
            ).upper(),
            "all_terminal_decision": _required_text(
                runtime, "all_terminal_decision"
            ).upper(),
        }
        expected = {
            "empty_evidence_decision": "HOLD",
            "non_terminal_decision": "REJECT",
            "all_terminal_decision": "ACCEPT",
        }
        if decisions != expected:
            raise MitigationSpecError(
                "closure_guard decisions must be HOLD / REJECT / ACCEPT"
            )

        terminal = _required_statuses(runtime, "terminal_statuses")
        blocking = _required_statuses(runtime, "blocking_statuses")
        overlap = terminal & blocking
        if overlap:
            raise MitigationSpecError(
                "terminal_statuses and blocking_statuses overlap: "
                + ", ".join(sorted(overlap))
            )

        spec = cls(
            schema_version=schema_version,
            mitigation_id=_required_text(value, "mitigation_id"),
            target_failure=_required_text(value, "target_failure"),
            guard_type=guard_type,
            safeguard_id=_required_text(runtime, "safeguard_id"),
            terminal_statuses=terminal,
            blocking_statuses=blocking,
            **decisions,
        )
        if spec.target_failure != "premature_parent_closure":
            raise MitigationSpecError(
                f"unsupported target_failure: {spec.target_failure!r}"
            )
        return spec

    def runtime_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mitigation_id": self.mitigation_id,
            "target_failure": self.target_failure,
            "runtime": {
                "guard_type": self.guard_type,
                "safeguard_id": self.safeguard_id,
                "terminal_statuses": sorted(self.terminal_statuses),
                "blocking_statuses": sorted(self.blocking_statuses),
                "empty_evidence_decision": self.empty_evidence_decision,
                "non_terminal_decision": self.non_terminal_decision,
                "all_terminal_decision": self.all_terminal_decision,
            },
        }

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.runtime_mapping(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_mitigation_spec(path: str | Path) -> MitigationSpec:
    """Load and validate a MitigationSpec JSON document."""

    with Path(path).open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, Mapping):
        raise MitigationSpecError("mitigation spec must be an object")
    return MitigationSpec.from_mapping(document)


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

    def __init__(self, mitigation_spec: MitigationSpec | None = None) -> None:
        self.mitigation_spec = mitigation_spec or MitigationSpec.default()
        self.safeguard_id = self.mitigation_spec.safeguard_id
        self.terminal_statuses = self.mitigation_spec.terminal_statuses
        self.blocking_statuses = self.mitigation_spec.blocking_statuses

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
                self.mitigation_spec.empty_evidence_decision,
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
                self.mitigation_spec.non_terminal_decision,
                f"Parent closure blocked by required child state: {blocker_text}.",
                1.0,
                {"child_state_present": True, "all_required_children_terminal": False},
                self.safeguard_id,
            )

        return Evaluation(
            self.mitigation_spec.all_terminal_decision,
            "All required child tasks are terminal.",
            1.0,
            {"child_state_present": True, "all_required_children_terminal": True},
            self.safeguard_id,
        )


class CompanionRuntime:
    """Observable event-to-state loop backed by a replayable event log."""

    supported_event_kinds = frozenset(
        {"agenda_item_upserted", "parent_closure_requested"}
    )

    def __init__(
        self,
        *,
        max_tasks_per_event: int = 1,
        event_store: JsonlEventStore | None = None,
        mitigation_spec: MitigationSpec | None = None,
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
        self.closure_guard = ClosureGuard(mitigation_spec)

    @classmethod
    def replay(
        cls,
        event_store: JsonlEventStore,
        *,
        max_tasks_per_event: int = 1,
        mitigation_spec: MitigationSpec | None = None,
    ) -> "CompanionRuntime":
        runtime = cls(
            max_tasks_per_event=max_tasks_per_event,
            event_store=event_store,
            mitigation_spec=mitigation_spec,
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
            "mitigation": {
                "mitigation_id": self.closure_guard.mitigation_spec.mitigation_id,
                "safeguard_id": self.closure_guard.safeguard_id,
                "spec_fingerprint": self.closure_guard.mitigation_spec.fingerprint,
            },
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

        if status in self.closure_guard.terminal_statuses:
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


def demo(
    event_store: JsonlEventStore | None = None,
    mitigation_spec: MitigationSpec | None = None,
) -> dict[str, Any]:
    runtime = (
        CompanionRuntime.replay(event_store, mitigation_spec=mitigation_spec)
        if event_store is not None
        else CompanionRuntime(mitigation_spec=mitigation_spec)
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


def _add_mitigation_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mitigation-spec",
        type=Path,
        help="validated MitigationSpec JSON produced by LLM Evaluation Lab",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="companion-mind",
        description="Persist events, run guarded state transitions, and replay state.",
    )
    subparsers = parser.add_subparsers(dest="command")

    demo_parser = subparsers.add_parser("demo", help="run the public stateful demo")
    demo_parser.add_argument("--event-log", type=Path)
    _add_mitigation_argument(demo_parser)

    replay_parser = subparsers.add_parser("replay", help="rebuild state from JSONL")
    replay_parser.add_argument("--event-log", type=Path, required=True)
    _add_mitigation_argument(replay_parser)

    ingest_parser = subparsers.add_parser("ingest", help="ingest one event JSON")
    ingest_parser.add_argument("--event-log", type=Path, required=True)
    ingest_parser.add_argument("--event", required=True, help="JSON path or - for stdin")
    _add_mitigation_argument(ingest_parser)

    validate_parser = subparsers.add_parser(
        "validate-mitigation",
        help="validate and fingerprint an executable MitigationSpec",
    )
    validate_parser.add_argument("--mitigation-spec", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        spec_path = getattr(args, "mitigation_spec", None)
        mitigation_spec = load_mitigation_spec(spec_path) if spec_path else None
        if args.command in (None, "demo"):
            path = getattr(args, "event_log", None)
            snapshot = demo(
                JsonlEventStore(path) if path else None,
                mitigation_spec=mitigation_spec,
            )
        elif args.command == "replay":
            snapshot = CompanionRuntime.replay(
                JsonlEventStore(args.event_log), mitigation_spec=mitigation_spec
            ).snapshot()
        elif args.command == "validate-mitigation":
            if mitigation_spec is None:
                raise MitigationSpecError("--mitigation-spec is required")
            snapshot = {
                "status": "VALID",
                "mitigation_id": mitigation_spec.mitigation_id,
                "safeguard_id": mitigation_spec.safeguard_id,
                "spec_fingerprint": mitigation_spec.fingerprint,
                "runtime": mitigation_spec.runtime_mapping()["runtime"],
            }
        else:
            store = JsonlEventStore(args.event_log)
            runtime = CompanionRuntime.replay(
                store, mitigation_spec=mitigation_spec
            )
            runtime.ingest(_load_event(args.event))
            snapshot = runtime.snapshot()
    except (
        EventLogError,
        MitigationSpecError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
