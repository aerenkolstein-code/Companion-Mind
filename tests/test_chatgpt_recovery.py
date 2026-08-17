import json
import unittest
from pathlib import Path

from companion_mind.chatgpt_recovery import (
    RecoveryError,
    build_recovery_artifact,
    canonical_json_bytes,
    reconcile_renderable_ledger,
    reconstruct_active_path,
    scan_credential_surfaces,
    verify_recovery_artifact,
)


def conversation_payload() -> dict[str, object]:
    return {
        "id": "synthetic-conversation",
        "title": "Synthetic C2 fixture",
        "mapping": {
            "root": {"parent": None, "children": ["u1"], "message": None},
            "u1": {
                "parent": "root",
                "children": ["tool1"],
                "message": {
                    "id": "m-user-1",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["hello"]},
                    "create_time": 1,
                    "metadata": {"turn_id": "turn-user-1"},
                },
            },
            "tool1": {
                "parent": "u1",
                "children": ["a1"],
                "message": {
                    "id": "m-tool-1",
                    "author": {"role": "tool"},
                    "content": {"content_type": "multimodal_text", "parts": [{"kind": "tool", "value": 7}]},
                    "create_time": 2,
                    "metadata": {},
                },
            },
            "a1": {
                "parent": "tool1",
                "children": [],
                "message": {
                    "id": "m-assistant-1",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "code", "parts": [{"language": "python", "text": "print(7)"}]},
                    "create_time": 3,
                    "metadata": {"dom_turn_id": "turn-assistant-1"},
                },
            },
            "branch": {
                "parent": "u1",
                "children": [],
                "message": {
                    "id": "m-branch",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["inactive branch"]},
                    "metadata": {},
                },
            },
        },
        "current_node": "a1",
    }


class ActivePathTest(unittest.TestCase):
    def test_parent_traversal_reconstructs_chronological_path(self) -> None:
        payload = conversation_payload()
        path = reconstruct_active_path(payload["mapping"], payload["current_node"])  # type: ignore[arg-type]
        self.assertEqual([node_id for node_id, _ in path], ["root", "u1", "tool1", "a1"])

    def test_cycle_fails_closed(self) -> None:
        payload = conversation_payload()
        payload["mapping"]["root"]["parent"] = "a1"  # type: ignore[index]
        with self.assertRaisesRegex(RecoveryError, "cycle"):
            reconstruct_active_path(payload["mapping"], payload["current_node"])  # type: ignore[arg-type]

    def test_missing_parent_fails_closed(self) -> None:
        payload = conversation_payload()
        payload["mapping"]["tool1"]["parent"] = "missing"  # type: ignore[index]
        with self.assertRaisesRegex(RecoveryError, "missing parent"):
            reconstruct_active_path(payload["mapping"], payload["current_node"])  # type: ignore[arg-type]


class ArtifactTest(unittest.TestCase):
    def test_root_tool_and_structured_payloads_are_lossless(self) -> None:
        artifact = build_recovery_artifact(conversation_payload(), exported_at="2026-08-17T00:00:00+00:00")
        self.assertEqual(artifact["mapping_size"], 5)
        self.assertEqual(artifact["active_path_size"], 4)
        self.assertIsNone(artifact["ordered_nodes"][0]["role"])
        self.assertEqual(artifact["ordered_nodes"][2]["role"], "tool")
        self.assertEqual(artifact["ordered_nodes"][2]["content_parts"], [{"kind": "tool", "value": 7}])
        self.assertEqual(artifact["ordered_nodes"][3]["content_type"], "code")

    def test_export_hash_is_deterministic_for_same_fixture_and_timestamp(self) -> None:
        first = build_recovery_artifact(conversation_payload(), exported_at="2026-08-17T00:00:00+00:00")
        second = build_recovery_artifact(conversation_payload(), exported_at="2026-08-17T00:00:00+00:00")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(verify_recovery_artifact(first)["credential_surface_count"], 0)

    def test_checksum_detects_mutation(self) -> None:
        artifact = build_recovery_artifact(conversation_payload(), exported_at="2026-08-17T00:00:00+00:00")
        artifact["ordered_nodes"][1]["role"] = "assistant"
        with self.assertRaisesRegex(RecoveryError, "checksum mismatch"):
            verify_recovery_artifact(artifact)

    def test_credential_shaped_envelope_fails_closed(self) -> None:
        payload = conversation_payload()
        payload["authorization"] = "Bearer secret"
        with self.assertRaisesRegex(RecoveryError, "credential/header-shaped"):
            build_recovery_artifact(payload, exported_at="2026-08-17T00:00:00+00:00")

    def test_scan_does_not_flag_ordinary_token_count(self) -> None:
        self.assertEqual(scan_credential_surfaces({"token_count": 42}), [])


class ReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.artifact = build_recovery_artifact(conversation_payload(), exported_at="2026-08-17T00:00:00+00:00")

    def test_stable_ids_match_without_text_similarity(self) -> None:
        ledger = [
            {"renderable_index": 0, "expected_role": "user", "known_turn_id_or_dom_id": "turn-user-1"},
            {"renderable_index": 1, "expected_role": "assistant", "known_turn_id_or_dom_id": "m-assistant-1"},
        ]
        result = reconcile_renderable_ledger(self.artifact, ledger)
        self.assertEqual(result.summary["MATCHED"], 2)
        self.assertEqual(result.summary["EXTRA_GRAPH_NODE"], 2)

    def test_missing_and_ambiguous_items_become_gap_ledger(self) -> None:
        ledger = [
            {"renderable_index": 0, "expected_role": "user", "known_turn_id_or_dom_id": "not-in-bulk"},
            {"renderable_index": 1, "expected_role": "assistant"},
        ]
        result = reconcile_renderable_ledger(self.artifact, ledger)
        self.assertEqual(result.summary["MISSING_FROM_BULK"], 1)
        self.assertEqual(result.summary["AMBIGUOUS_MAPPING"], 1)
        self.assertEqual(len(result.gaps), 2)
        self.assertEqual(result.gaps[0]["recommended_backfill_action"], "sidebar_random_access")
        self.assertEqual(result.gaps[1]["recommended_backfill_action"], "capture_stable_id_then_retry")

    def test_role_mismatch_is_ambiguous_not_silently_matched(self) -> None:
        ledger = [
            {"renderable_index": 0, "expected_role": "assistant", "known_turn_id_or_dom_id": "turn-user-1"},
        ]
        result = reconcile_renderable_ledger(self.artifact, ledger)
        self.assertEqual(result.summary["AMBIGUOUS_MAPPING"], 1)


class BrowserAdapterStaticSafetyTest(unittest.TestCase):
    def test_browser_adapter_has_no_bulk_network_or_queryclient_mutation_calls(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "tools" / "chatgpt_recovery_exporter.js").read_text(encoding="utf-8")
        forbidden_calls = [
            "fetch(", "XMLHttpRequest(", ".setQueryData(", ".removeQueries(", ".resetQueries(",
            ".invalidateQueries(", ".clear(",
        ]
        for call in forbidden_calls:
            self.assertNotIn(call, source)


if __name__ == "__main__":
    unittest.main()
