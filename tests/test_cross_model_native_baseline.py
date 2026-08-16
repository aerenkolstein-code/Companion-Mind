from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cross_model_native_baseline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("cross_model_native_baseline", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load cross-model diagnostic script")
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
                "user_prompt": f"离线用户输入 {turn}",
                "reference_response": f"离线参考回答 {turn}",
            }
            for turn in range(1, 21)
        ],
    }


class FakeGenerator:
    def __init__(self, module) -> None:
        self.module = module
        self.requests: list[tuple[object, tuple[object, ...]]] = []

    def __call__(self, spec, messages):
        self.requests.append((spec, tuple(messages)))
        turn = len(self.requests)
        return self.module.NativeResponse(
            provider=spec.provider,
            requested_model=spec.model,
            returned_model=spec.model,
            content=f"连续关系中的离线响应 {turn}",
            response_id=f"response-{turn}",
            usage={
                "prompt_tokens": len(messages) * 10,
                "completion_tokens": 5,
                "total_tokens": len(messages) * 10 + 5,
                "private_text": "must be removed",
            },
        )


class CrossModelNativeBaselineTests(unittest.TestCase):
    def _specs(self, module):
        return (
            module.ProviderSpec(
                key="glm_45_air",
                provider="zhipu",
                model="glm-4.5-air",
                endpoint="https://open.bigmodel.cn/api/paas/v4/chat/completions",
                api_key="test-glm-key",
                thinking_mode="disabled",
            ),
            module.ProviderSpec(
                key="glm_41v_thinking_flashx",
                provider="zhipu",
                model="glm-4.1v-thinking-flashx",
                endpoint="https://open.bigmodel.cn/api/paas/v4/chat/completions",
                api_key="test-glm-key",
                thinking_mode="model_default_built_in",
            ),
            module.ProviderSpec(
                key="glm_47_flash",
                provider="zhipu",
                model="glm-4.7-flash",
                endpoint="https://open.bigmodel.cn/api/paas/v4/chat/completions",
                api_key="test-glm-key",
                thinking_mode="disabled",
            ),
        )

    def test_payloads_lock_equal_sampling_and_respect_thinking_contracts(self) -> None:
        module = _load_module()
        message = module.ProviderMessage(role="user", content="hello")
        glm_45, glm_41v, glm_47 = self._specs(module)
        payloads = [
            module._request_payload(spec, [message])
            for spec in (glm_45, glm_41v, glm_47)
        ]

        for payload in payloads:
            self.assertEqual(payload["temperature"], 1.0)
            self.assertEqual(payload["max_tokens"], 4096)
            self.assertNotIn("tools", payload)
        self.assertEqual(payloads[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("thinking", payloads[1])
        self.assertEqual(payloads[2]["thinking"], {"type": "disabled"})

    def test_exactly_three_full_history_native_runs_are_content_safe(self) -> None:
        module = _load_module()
        generator = FakeGenerator(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            private_path = Path(temp_dir) / "private.json"
            blind_path = Path(temp_dir) / "blind.json"
            public_path = Path(temp_dir) / "public.json"
            report = module.run_diagnostic(
                self._specs(module),
                _corpus_document(),
                corpus_sha256="a" * 64,
                personas_dir=ROOT / "personas",
                private_output=private_path,
                blind_output=blind_path,
                public_output=public_path,
                generate=generator,
            )
            private_report = json.loads(private_path.read_text(encoding="utf-8"))
            blind_report = json.loads(blind_path.read_text(encoding="utf-8"))
            public_report = json.loads(public_path.read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["api_call_count"], 60)
        self.assertEqual(len(generator.requests), 60)
        self.assertEqual(len(generator.requests[19][1]), 40)
        self.assertEqual(len(generator.requests[39][1]), 40)
        self.assertEqual(len(generator.requests[59][1]), 40)
        self.assertEqual(generator.requests[19][1][1].content, "离线用户输入 1")
        self.assertEqual(generator.requests[39][1][1].content, "离线用户输入 1")
        self.assertEqual(generator.requests[59][1][1].content, "离线用户输入 1")
        self.assertFalse(
            any(
                "离线参考回答" in message.content
                for _, messages in generator.requests
                for message in messages
            )
        )
        for key in (
            "glm_45_air",
            "glm_41v_thinking_flashx",
            "glm_47_flash",
        ):
            self.assertTrue(report["results"][key]["turn_1_present_at_turn_20"])
            self.assertEqual(report["results"][key]["turn_20_prompt_tokens"], 400)
        self.assertEqual(report["semantic_gate"], "PENDING_BLIND_REVIEW")
        self.assertEqual(
            report["results"]["glm_45_air"]["semantic_relationship_label"],
            "PENDING_BLIND_REVIEW",
        )
        public_text = json.dumps(public_report, ensure_ascii=False)
        self.assertNotIn("离线用户输入", public_text)
        self.assertNotIn("离线参考回答", public_text)
        self.assertNotIn("连续关系中的离线响应", public_text)
        self.assertNotIn("private_text", public_text)
        self.assertEqual(
            private_report["results"]["glm_45_air"]["turns"][0]["user_prompt"],
            "离线用户输入 1",
        )
        self.assertEqual(
            set(blind_report["trajectories"]),
            {"MODEL_A", "MODEL_B", "MODEL_C"},
        )
        blind_text = json.dumps(blind_report, ensure_ascii=False)
        self.assertNotIn("glm-4.5-air", blind_text)
        self.assertNotIn("glm-4.1v-thinking-flashx", blind_text)
        self.assertNotIn("glm-4.7-flash", blind_text)
        self.assertNotIn("zhipu", blind_text)

    def test_diagnostic_rejects_any_model_set_other_than_locked_three(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "requires exactly glm-4.5-air"):
                module.run_diagnostic(
                    self._specs(module)[:1],
                    _corpus_document(),
                    corpus_sha256="a" * 64,
                    personas_dir=ROOT / "personas",
                    private_output=Path(temp_dir) / "private.json",
                    blind_output=Path(temp_dir) / "blind.json",
                    public_output=Path(temp_dir) / "public.json",
                    generate=FakeGenerator(module),
                )

    def test_provider_error_never_echoes_key_or_response_body(self) -> None:
        module = _load_module()
        with self.assertRaisesRegex(module.DiagnosticProviderError, "API key is required"):
            module.ProviderSpec(
                key="glm_45_air",
                provider="zhipu",
                model="glm-4.5-air",
                endpoint="https://example.test/chat/completions",
                api_key="",
                thinking_mode="disabled",
            )


if __name__ == "__main__":
    unittest.main()
