import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from companion_mind.models import Event
from companion_mind.runtime import (
    ClosureGuard,
    CompanionRuntime,
    EventLogError,
    JsonlEventStore,
    main,
)


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

    def agenda_event(self, event_id: str, status: str) -> Event:
        return Event(
            event_id=event_id,
            kind="agenda_item_upserted",
            payload={
                "item_id": "qualification",
                "parent_id": "onboarding",
                "status": status,
                "required": True,
            },
            source="public-test-fixture",
        )

    def state_closure_event(self, event_id: str) -> Event:
        return Event(
            event_id=event_id,
            kind="parent_closure_requested",
            payload={"parent_id": "onboarding"},
            source="public-test-fixture",
        )

    def test_state_backed_open_agenda_blocks_closure(self) -> None:
        runtime = CompanionRuntime()
        runtime.ingest(self.agenda_event("evt-agenda-open", "OPEN"))
        traces = runtime.ingest(self.state_closure_event("evt-state-check"))

        self.assertIn("qualification", runtime.agenda)
        self.assertEqual(traces[0].evaluation.decision, "REJECT")
        self.assertEqual(runtime.state["parent_status"]["onboarding"], "OPEN")

    def test_terminal_update_clears_agenda_and_allows_closure(self) -> None:
        runtime = CompanionRuntime()
        runtime.ingest(self.agenda_event("evt-agenda-open", "OPEN"))
        runtime.ingest(self.agenda_event("evt-agenda-done", "DONE"))
        traces = runtime.ingest(self.state_closure_event("evt-state-close"))

        self.assertEqual(runtime.agenda, {})
        self.assertEqual(traces[0].evaluation.decision, "ACCEPT")
        self.assertEqual(runtime.state["parent_status"]["onboarding"], "DONE")


class EventContractTest(unittest.TestCase):
    def test_event_mapping_requires_fields_and_object_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing event field"):
            Event.from_mapping({"event_id": "evt", "kind": "test", "payload": {}})
        with self.assertRaisesRegex(ValueError, "payload must be an object"):
            Event.from_mapping(
                {"event_id": "evt", "kind": "test", "payload": [], "source": "test"}
            )
        with self.assertRaisesRegex(ValueError, "event_id must be"):
            Event.from_mapping(
                {"event_id": None, "kind": "test", "payload": {}, "source": "test"}
            )


class EventStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "events.jsonl"
        self.store = JsonlEventStore(self.path)

    @staticmethod
    def event(event_id: str, status: str = "OPEN") -> Event:
        return Event(
            event_id=event_id,
            kind="agenda_item_upserted",
            payload={
                "item_id": "qualification",
                "parent_id": "onboarding",
                "status": status,
                "required": True,
            },
            source="public-test-fixture",
        )

    def test_replay_reconstructs_identical_snapshot(self) -> None:
        live = CompanionRuntime(event_store=self.store)
        live.ingest(self.event("evt-one", "OPEN"))
        live.ingest(
            Event(
                "evt-two",
                "parent_closure_requested",
                {"parent_id": "onboarding"},
                "public-test-fixture",
            )
        )

        replayed = CompanionRuntime.replay(self.store)
        self.assertEqual(replayed.snapshot(), live.snapshot())

    def test_duplicate_event_is_not_appended(self) -> None:
        runtime = CompanionRuntime(event_store=self.store)
        event = self.event("evt-once")
        runtime.ingest(event)
        runtime.ingest(event)
        self.assertEqual(len(self.store.read()), 1)
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_corrupt_log_fails_closed(self) -> None:
        self.path.write_text("{not-json}\n", encoding="utf-8")
        with self.assertRaises(EventLogError):
            self.store.read()

    def test_invalid_event_is_not_persisted(self) -> None:
        runtime = CompanionRuntime(event_store=self.store)
        invalid = Event(
            "evt-invalid",
            "agenda_item_upserted",
            {"parent_id": "onboarding"},
            "public-test-fixture",
        )
        with self.assertRaises(ValueError):
            runtime.ingest(invalid)
        self.assertFalse(self.path.exists())

        malformed_children = Event(
            "evt-malformed",
            "parent_closure_requested",
            {"parent_id": "onboarding", "children": ["not-an-object"]},
            "public-test-fixture",
        )
        with self.assertRaises(ValueError):
            runtime.ingest(malformed_children)
        self.assertFalse(self.path.exists())

    def test_cli_demo_and_replay_match(self) -> None:
        live_output = io.StringIO()
        with redirect_stdout(live_output):
            self.assertEqual(
                main(["demo", "--event-log", str(self.path)]),
                0,
            )
        replay_output = io.StringIO()
        with redirect_stdout(replay_output):
            self.assertEqual(
                main(["replay", "--event-log", str(self.path)]),
                0,
            )
        self.assertEqual(
            json.loads(live_output.getvalue()),
            json.loads(replay_output.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
