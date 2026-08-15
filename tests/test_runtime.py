import unittest

from companion_mind.models import Event
from companion_mind.runtime import ClosureGuard, CompanionRuntime


class ClosureGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = ClosureGuard()

    def test_open_child_blocks_parent_closure(self) -> None:
        result = self.guard.evaluate(
            [
                {"child_id": "quick-check", "status": "DONE"},
                {"child_id": "qualification", "status": "OPEN"},
            ]
        )
        self.assertEqual(result.decision, "REJECT")
        self.assertEqual(result.safeguard_id, "CM-GUARD-001")

    def test_unknown_or_missing_status_fails_closed(self) -> None:
        self.assertEqual(
            self.guard.evaluate([{"child_id": "evidence"}]).decision,
            "REJECT",
        )
        self.assertEqual(self.guard.evaluate([]).decision, "HOLD")

    def test_all_terminal_children_allow_closure(self) -> None:
        result = self.guard.evaluate(
            [
                {"child_id": "one", "status": "DONE"},
                {"child_id": "two", "status": "CANCELLED"},
            ]
        )
        self.assertEqual(result.decision, "ACCEPT")

    def test_order_and_wording_do_not_change_the_decision(self) -> None:
        first = [
            {"child_id": "send screenshot", "status": "DONE"},
            {"child_id": "complete qualification", "status": "OPEN"},
        ]
        second = list(reversed(first))
        self.assertEqual(self.guard.evaluate(first).decision, "REJECT")
        self.assertEqual(self.guard.evaluate(second).decision, "REJECT")


class RuntimeTest(unittest.TestCase):
    def event(self, event_id: str, children: list[dict[str, str]]) -> Event:
        return Event(
            event_id=event_id,
            kind="parent_closure_requested",
            payload={"parent_id": "onboarding", "children": children},
            source="public-test-fixture",
        )

    def test_rejected_candidate_cannot_write_done(self) -> None:
        runtime = CompanionRuntime()
        traces = runtime.ingest(
            self.event(
                "evt-open",
                [
                    {"child_id": "quick-check", "status": "DONE"},
                    {"child_id": "qualification", "status": "OPEN"},
                ],
            )
        )
        self.assertEqual(traces[0].evaluation.decision, "REJECT")
        self.assertEqual(runtime.state["parent_status"]["onboarding"], "OPEN")
        self.assertEqual(traces[0].state_writes, ())

    def test_accepted_candidate_writes_sourced_delta(self) -> None:
        runtime = CompanionRuntime()
        traces = runtime.ingest(
            self.event(
                "evt-done",
                [
                    {"child_id": "quick-check", "status": "DONE"},
                    {"child_id": "qualification", "status": "DONE"},
                ],
            )
        )
        self.assertEqual(traces[0].evaluation.decision, "ACCEPT")
        self.assertEqual(runtime.state["parent_status"]["onboarding"], "DONE")
        self.assertEqual(traces[0].state_writes[0].source_event_id, "evt-done")

    def test_duplicate_event_is_not_processed_twice(self) -> None:
        runtime = CompanionRuntime()
        event = self.event(
            "evt-once", [{"child_id": "qualification", "status": "OPEN"}]
        )
        self.assertEqual(len(runtime.ingest(event)), 1)
        self.assertEqual(runtime.ingest(event), [])
        self.assertEqual(len(runtime.traces), 1)


if __name__ == "__main__":
    unittest.main()

