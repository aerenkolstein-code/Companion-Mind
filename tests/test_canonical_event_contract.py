from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from companion_mind.contracts.canonical_event_v1 import (
    CanonicalEvent,
    ContractValidationError,
    authority_snapshot_after_event,
    canonical_order_key,
    duplicate_event_ids,
    knowledge_state,
    stable_identity,
    validate_canonical_event,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "canonical_event_v1"
SCHEMA_PATH = ROOT / "schemas" / "canonical_event_v1.schema.json"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class CanonicalEventContractTests(unittest.TestCase):
    def test_machine_schema_declares_required_contract_surface(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        required = set(schema["required"])
        expected = {
            "event_id", "session_id", "turn_id", "sequence_no", "actor_role",
            "message_id", "persona_id", "relationship_id", "provider", "model",
            "observed_at", "created_at", "content_type", "content_payload", "status",
            "source_ref", "attachment_ref", "correction_id", "correction_of",
            "redaction_state", "metadata",
        }
        self.assertEqual(required, expected)
        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["complete", "partial", "failed"],
        )
        knowledge_states = schema["$defs"]["knowledgeValue"]["properties"]["state"]["enum"]
        self.assertEqual(
            set(knowledge_states),
            {"KNOWN_VALUE", "KNOWN_EMPTY", "UNKNOWN", "N_A", "NOT_LOOKED_UP"},
        )

    def test_all_public_safe_fixtures_validate(self) -> None:
        for path in sorted(FIXTURES.glob("*.json")):
            with self.subTest(path=path.name):
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(CanonicalEvent.from_mapping(value).to_mapping(), value)

    def test_stable_identity_survives_provider_model_switch(self) -> None:
        original = load_fixture("01_owned_client_user_text.json")
        switched = deepcopy(original)
        switched["provider"] = "synthetic-provider-b"
        switched["model"] = "synthetic-model-b"
        switched["event_id"] = "evt-user-provider-switch"
        self.assertEqual(stable_identity(original), stable_identity(switched))
        self.assertEqual(
            stable_identity(original),
            ("persona-synthetic-001", "relationship-synthetic-001"),
        )

    def test_deterministic_order_and_duplicate_detection(self) -> None:
        first = load_fixture("01_owned_client_user_text.json")
        second = load_fixture("02_owned_client_assistant_text.json")
        shuffled = [second, first]
        ordered = sorted(shuffled, key=canonical_order_key)
        self.assertEqual([item["event_id"] for item in ordered], [first["event_id"], second["event_id"]])
        self.assertEqual(duplicate_event_ids([first, second, deepcopy(first)]), {first["event_id"]})

    def test_correction_is_append_only_reference(self) -> None:
        original = load_fixture("02_owned_client_assistant_text.json")
        correction = load_fixture("06_correction.json")
        before = deepcopy(original)
        self.assertEqual(correction["correction_of"], original["event_id"])
        self.assertNotEqual(correction["event_id"], original["event_id"])
        validate_canonical_event(correction)
        self.assertEqual(original, before)

        invalid = deepcopy(correction)
        invalid["correction_id"] = None
        with self.assertRaises(ContractValidationError):
            validate_canonical_event(invalid)

    def test_complete_partial_failed_are_distinct(self) -> None:
        complete = load_fixture("02_owned_client_assistant_text.json")
        partial = load_fixture("04_partial_assistant.json")
        failed = load_fixture("05_failed_assistant.json")
        self.assertEqual({complete["status"], partial["status"], failed["status"]}, {"complete", "partial", "failed"})
        for value in (complete, partial, failed):
            validate_canonical_event(value)

    def test_structured_multimodal_and_provenance_round_trip(self) -> None:
        multimodal = load_fixture("03_browser_sidecar_multimodal.json")
        structured = load_fixture("07_structured_tool_payload.json")
        for value in (multimodal, structured):
            round_tripped = json.loads(json.dumps(CanonicalEvent.from_mapping(value).to_mapping()))
            self.assertEqual(round_tripped["content_payload"], value["content_payload"])
            self.assertEqual(round_tripped["source_ref"], value["source_ref"])
            self.assertEqual(round_tripped["attachment_ref"], value["attachment_ref"])

    def test_secret_exclusion_requires_redaction(self) -> None:
        redacted = load_fixture("08_redacted_secret_like.json")
        validate_canonical_event(redacted)
        self.assertEqual(redacted["content_payload"]["api_key"], "[SECRET_REDACTED]")

        bad = deepcopy(redacted)
        bad["content_payload"]["api_key"] = "synthetic-unredacted-value"
        bad["redaction_state"] = "none"
        with self.assertRaises(ContractValidationError):
            validate_canonical_event(bad)

    def test_unknown_semantics_do_not_collapse(self) -> None:
        unknown = load_fixture("09_unknown_semantics.json")
        self.assertEqual(knowledge_state(unknown, "relationship_status"), "UNKNOWN")

        known_empty = deepcopy(unknown)
        known_empty["event_id"] = "evt-known-empty"
        known_empty["metadata"]["knowledge"]["relationship_status"] = {"state": "KNOWN_EMPTY"}
        self.assertEqual(knowledge_state(known_empty, "relationship_status"), "KNOWN_EMPTY")
        self.assertNotEqual(
            knowledge_state(unknown, "relationship_status"),
            knowledge_state(known_empty, "relationship_status"),
        )

        smuggled = deepcopy(unknown)
        smuggled["metadata"]["knowledge"]["relationship_status"] = {
            "state": "UNKNOWN",
            "value": "secretly-known",
        }
        with self.assertRaises(ContractValidationError):
            validate_canonical_event(smuggled)

    def test_a018_a019_a020_share_one_conformance_target(self) -> None:
        fixtures = {
            "A019": "01_owned_client_user_text.json",
            "A018": "03_browser_sidecar_multimodal.json",
            "A020": "10_historical_backfill_import.json",
        }
        for adapter, name in fixtures.items():
            with self.subTest(adapter=adapter):
                event = validate_canonical_event(load_fixture(name))
                self.assertEqual(event["metadata"]["adapter"], adapter)

        imported = load_fixture("10_historical_backfill_import.json")
        self.assertEqual(imported["source_ref"]["observation_type"], "imported")
        masquerade = deepcopy(imported)
        masquerade["source_ref"]["observation_type"] = "observed"
        with self.assertRaises(ContractValidationError):
            validate_canonical_event(masquerade)

    def test_journal_cannot_mutate_persona_relationship_authority(self) -> None:
        event = load_fixture("02_owned_client_assistant_text.json")
        snapshot = {
            "persona": {"persona_id": "persona-synthetic-001", "biography": "frozen"},
            "relationship": {"relationship_id": "relationship-synthetic-001", "status": "frozen"},
        }
        result = authority_snapshot_after_event(event, snapshot)
        self.assertEqual(result, snapshot)
        self.assertIsNot(result, snapshot)

        invalid = deepcopy(event)
        invalid["metadata"]["extensions"] = {
            "persona_current": {"biography": "attempted direct mutation"}
        }
        with self.assertRaises(ContractValidationError):
            validate_canonical_event(invalid)

    def test_provider_model_metadata_cannot_replace_stable_ids(self) -> None:
        event = load_fixture("02_owned_client_assistant_text.json")
        identity = stable_identity(event)
        for provider, model in (
            ("provider-x", "model-x"),
            ("provider-y", "model-y"),
        ):
            candidate = deepcopy(event)
            candidate["event_id"] = f"evt-{provider}"
            candidate["provider"] = provider
            candidate["model"] = model
            self.assertEqual(stable_identity(candidate), identity)

    def test_fixture_tree_contains_no_unredacted_secret_fields(self) -> None:
        for path in sorted(FIXTURES.glob("*.json")):
            with self.subTest(path=path.name):
                value = json.loads(path.read_text(encoding="utf-8"))
                validate_canonical_event(value)


if __name__ == "__main__":
    unittest.main()
