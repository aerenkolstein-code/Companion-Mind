"""A deterministic continuous-cognition loop with an executable Closure Guard."""

from __future__ import annotations

from dataclasses import asdict
from itertools import count
from typing import Any, Iterable, Mapping

from .models import (
    BeliefCandidate,
    CognitiveTask,
    DecisionTrace,
    Evaluation,
    Event,
    StateDelta,
)


class ClosureGuard:
    """Reject premature parent closure while required child work remains.

    The guard evaluates structured child state, not wording or child order. It
    is the runtime implementation linked to EVAL-CASE-001 in LLM Evaluation Lab.
    """

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
    """Minimal observable loop used by the first closed-loop evaluation."""

    def __init__(self, *, max_tasks_per_event: int = 1) -> None:
        if max_tasks_per_event < 1:
            raise ValueError("max_tasks_per_event must be positive")
        self.state: dict[str, Any] = {}
        self.agenda: dict[str, CognitiveTask] = {}
        self.traces: list[DecisionTrace] = []
        self._processed_events: set[str] = set()
        self._processed_tasks: set[str] = set()
        self._ids = count(1)
        self._max_tasks_per_event = max_tasks_per_event
        self.closure_guard = ClosureGuard()

    def ingest(self, event: Event) -> list[DecisionTrace]:
        """Process one event once and return the resulting decision traces."""

        if event.event_id in self._processed_events:
            return []
        self._processed_events.add(event.event_id)

        event_deltas = self._apply_event(event)
        tasks = self._discover_tasks(event, event_deltas)[: self._max_tasks_per_event]
        traces: list[DecisionTrace] = []

        for task in tasks:
            if task.dedupe_key in self._processed_tasks:
                continue
            self._processed_tasks.add(task.dedupe_key)
            self.agenda[task.task_id] = task
            candidate = self._reason(task, event)
            evaluation = self._evaluate(task, candidate, event)
            writes = self._commit(candidate, evaluation, event)
            trace = DecisionTrace(
                event_id=event.event_id,
                task_id=task.task_id,
                belief_id=candidate.belief_id,
                evaluation=evaluation,
                state_writes=tuple(writes),
            )
            self.traces.append(trace)
            traces.append(trace)
            self.agenda.pop(task.task_id, None)

        return traces

    def _apply_event(self, event: Event) -> list[StateDelta]:
        parent_id = str(event.payload.get("parent_id", "parent-goal"))
        if "parent_status" not in self.state:
            self.state["parent_status"] = {parent_id: "OPEN"}
        previous = self.state.get("last_event_id")
        self.state["last_event_id"] = event.event_id
        return [
            StateDelta(
                key="last_event_id",
                old_value=previous,
                new_value=event.event_id,
                source_event_id=event.event_id,
                cause="event_ingest",
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
            children = event.payload.get("children", [])
            return self.closure_guard.evaluate(children)
        return Evaluation(
            "HOLD",
            "No evaluator is registered for this task type.",
            0.0,
            {"evaluator_registered": False},
        )

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


def demo() -> dict[str, Any]:
    runtime = CompanionRuntime()
    traces = runtime.ingest(
        Event(
            event_id="evt-demo-001",
            kind="parent_closure_requested",
            payload={
                "parent_id": "onboarding",
                "children": [
                    {"child_id": "quick-check", "status": "DONE"},
                    {"child_id": "qualification", "status": "OPEN"},
                ],
            },
            source="synthetic-public-demo",
        )
    )
    return {"state": runtime.state, "traces": [asdict(trace) for trace in traces]}


if __name__ == "__main__":
    import json

    print(json.dumps(demo(), ensure_ascii=False, indent=2, sort_keys=True))

