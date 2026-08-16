from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "glm_runtime_v2_recovery.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("glm_runtime_v2_recovery", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load ENG-DIAG-03 script")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _corpus_document() -> dict:
    return {
        "schema_version": "lin-zhiyao-private-corpus/v1",
        "pairs": [
            {
                "turn": turn,
                "user_prompt": f"私有用户输入 {turn}",
                "reference_response": f"私有参考回答 {turn}",
            }
            for turn in range(1, 21)
        ],
    }


class FakeDualLaneGenerator:
    def __init__(self, module, *, fail_observer_at: int | None = None) -> None:
        self.module = module
        self.fail_observer_at = fail_observer_at
        self.requests = []
        self.persona_calls = 0
        self.observer_calls = 0

    def __call__(self, spec, messages):
        frozen = tuple(messages)
        self.requests.append((spec, frozen))
        is_observer = frozen[0].content.startswith("STATE_OBSERVER_V2")
        if is_observer:
            self.observer_calls += 1
            if self.fail_observer_at == self.observer_calls:
                raise self.module.DiagnosticProviderError("fake observer outage")
            observer_payload = json.loads(frozen[1].content)
            current = observer_payload["current_turn"]
            content = json.dumps(
                {
                    "changes": [
                        {
                            "field": "conversation.active_topic",
                            "operation": "set",
                            "value": f"连续主题 {self.observer_calls}",
                            "evidence_event_ids": [
                                current["user"]["event_id"],
                                current["assistant"]["event_id"],
                            ],
                            "confidence": "high",
                            "reason": "当前轮有明确主题证据",
                        }
                    ]
                },
                ensure_ascii=False,
            )
            call_number = self.observer_calls
            lane = "observer"
        else:
            self.persona_calls += 1
            content = f"既有关系中的私有生成回答 {self.persona_calls}"
            call_number = self.persona_calls
            lane = "persona"
        return self.module.NativeResponse(
            provider=spec.provider,
            requested_model=spec.model,
            returned_model=spec.model,
            content=content,
            response_id=f"{lane}-{call_number}",
            usage={
                "prompt_tokens": len(frozen) * 10,
                "completion_tokens": 5,
                "total_tokens": len(frozen) * 10 + 5,
                "private_usage_detail": "must be removed",
            },
        )


class GLMRuntimeV2RecoveryTests(unittest.TestCase):
    def _spec(self, module):
        return module.ProviderSpec(
            key="glm_47_flash",
            provider="zhipu",
            model="glm-4.7-flash",
            endpoint="https://open.bigmodel.cn/api/paas/v4/chat/completions",
            api_key="test-glm-secret-key",
            thinking_mode="disabled",
        )

    def _run(self, module, generator):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        report = module.run_diagnostic(
            self._spec(module),
            _corpus_document(),
            corpus_sha256=module.EXPECTED_CORPUS_SHA256,
            personas_dir=ROOT / "personas",
            work_dir=root / "runtime",
            private_output=root / "private.json",
            blind_output=root / "blind.json",
            public_output=root / "public.json",
            generate=generator,
            execution_commit="offline-test-commit",
            run_id="offline-test-run",
        )
        return temporary, root, report

    def test_frozen_persona_request_contract_is_unchanged(self) -> None:
        module = _load_module()
        payload = module._request_payload(
            self._spec(module),
            [module.ProviderMessage(role="user", content="hello")],
        )
        self.assertEqual(payload["model"], "glm-4.7-flash")
        self.assertEqual(payload["temperature"], 1.0)
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertFalse(payload["stream"])
        self.assertNotIn("tools", payload)

    def test_one_trajectory_makes_exactly_twenty_plus_twenty_calls(self) -> None:
        module = _load_module()
        generator = FakeDualLaneGenerator(module)
        temporary, root, report = self._run(module, generator)
        try:
            private_report = json.loads((root / "private.json").read_text())
            blind_report = json.loads((root / "blind.json").read_text())
            public_report = json.loads((root / "public.json").read_text())
        finally:
            temporary.cleanup()

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(generator.persona_calls, 20)
        self.assertEqual(generator.observer_calls, 20)
        self.assertEqual(len(generator.requests), 40)
        self.assertEqual(
            report["actual_calls"],
            {
                "persona_attempted": 20,
                "persona_succeeded": 20,
                "observer_attempted": 20,
                "observer_succeeded": 20,
                "observer_failed": 0,
                "total": 40,
            },
        )
        self.assertEqual(report["runtime"]["turn_count"], 20)
        self.assertEqual(report["runtime"]["raw_event_count"], 40)
        self.assertEqual(report["runtime"]["assistant_event_count"], 20)
        self.assertEqual(report["runtime"]["turn_20_message_count"], 40)
        self.assertTrue(report["runtime"]["turn_1_present_at_turn_20"])
        self.assertTrue(report["runtime"]["exact_replay"])
        self.assertEqual(
            report["observer_telemetry"]["accepted_delta_count"], 20
        )
        self.assertEqual(
            report["observer_telemetry"]["accepted_fields"],
            {"conversation.active_topic": 20},
        )
        self.assertEqual(private_report["actual_calls"]["total"], 40)
        self.assertEqual(len(private_report["delta_records"]), 20)
        self.assertEqual(set(blind_report["trajectories"]), {"MODEL_X"})
        self.assertEqual(public_report, report)

    def test_observer_receives_only_previous_state_and_current_turn(self) -> None:
        module = _load_module()
        generator = FakeDualLaneGenerator(module)
        temporary, _, _ = self._run(module, generator)
        try:
            observer_requests = [
                messages
                for _, messages in generator.requests
                if messages[0].content.startswith("STATE_OBSERVER_V2")
            ]
            self.assertEqual(len(observer_requests), 20)
            self.assertTrue(all(len(messages) == 2 for messages in observer_requests))
            second = json.loads(observer_requests[1][1].content)
            self.assertEqual(second["current_turn"]["user"]["turn_index"], 2)
            self.assertEqual(second["current_turn"]["assistant"]["turn_index"], 2)
            self.assertNotIn("history", second)
            self.assertNotIn("私有用户输入 1", observer_requests[1][1].content)
            self.assertNotIn("私有参考回答", observer_requests[1][1].content)
        finally:
            temporary.cleanup()

    def test_first_observer_failure_stops_without_hidden_retry(self) -> None:
        module = _load_module()
        generator = FakeDualLaneGenerator(module, fail_observer_at=5)
        temporary, root, report = self._run(module, generator)
        try:
            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(generator.persona_calls, 5)
            self.assertEqual(generator.observer_calls, 5)
            self.assertEqual(len(generator.requests), 10)
            self.assertEqual(report["actual_calls"]["total"], 10)
            self.assertEqual(report["actual_calls"]["observer_failed"], 1)
            self.assertIn("OBSERVER_CALL_FAILED", report["structural_failures"])
            self.assertEqual(
                report["observer_telemetry"]["failure_categories"],
                {"provider_failure": 1},
            )
            self.assertTrue((root / "private.json").is_file())
            self.assertTrue((root / "blind.json").is_file())
            self.assertTrue((root / "public.json").is_file())
        finally:
            temporary.cleanup()

    def test_fingerprint_and_exact_model_fail_closed_before_calls(self) -> None:
        module = _load_module()
        generator = FakeDualLaneGenerator(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "corpus fingerprint"):
                module.run_diagnostic(
                    self._spec(module),
                    _corpus_document(),
                    corpus_sha256="0" * 64,
                    personas_dir=ROOT / "personas",
                    work_dir=root / "runtime",
                    private_output=root / "private.json",
                    blind_output=root / "blind.json",
                    public_output=root / "public.json",
                    generate=generator,
                )
        self.assertEqual(generator.requests, [])

        wrong = module.ProviderSpec(
            key="glm_47_flash",
            provider="zhipu",
            model="glm-other",
            endpoint="https://example.test/chat/completions",
            api_key="test-key",
            thinking_mode="disabled",
        )
        with self.assertRaisesRegex(ValueError, "exact model"):
            module.GLMRuntimeV2StateObserver(wrong)

    def test_public_report_contains_no_private_text_or_secret(self) -> None:
        module = _load_module()
        generator = FakeDualLaneGenerator(module)
        temporary, root, report = self._run(module, generator)
        try:
            public_text = (root / "public.json").read_text(encoding="utf-8")
            for forbidden in (
                "私有用户输入",
                "私有参考回答",
                "私有生成回答",
                "连续主题",
                "test-glm-secret-key",
                "user_prompt",
                "reference_response",
                "generated_response",
                "delta_records",
            ):
                self.assertNotIn(forbidden, public_text)
            self.assertFalse(report["content_logged"])
            self.assertTrue(report["structural_gates"]["public_content_safe"])
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
