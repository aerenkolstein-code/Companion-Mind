from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "glm_runtime_lift.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("glm_runtime_lift", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load GLM Runtime lift script")
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
        self.requests = []

    def __call__(self, spec, messages):
        self.requests.append((spec, tuple(messages)))
        turn = len(self.requests)
        return self.module.NativeResponse(
            provider=spec.provider,
            requested_model=spec.model,
            returned_model=spec.model,
            content=f"连续关系中的离线响应 {turn}",
            response_id=f"runtime-{turn}",
            usage={
                "prompt_tokens": len(messages) * 10,
                "completion_tokens": 5,
                "total_tokens": len(messages) * 10 + 5,
                "private_text": "must be removed",
            },
        )


class GLMRuntimeLiftTests(unittest.TestCase):
    def _spec(self, module):
        return module.ProviderSpec(
            key="glm_47_flash",
            provider="zhipu",
            model="glm-4.7-flash",
            endpoint="https://open.bigmodel.cn/api/paas/v4/chat/completions",
            api_key="test-glm-key",
            thinking_mode="disabled",
        )

    def test_request_contract_matches_frozen_native_sampling(self) -> None:
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

    def test_exactly_one_twenty_turn_runtime_run_is_content_safe(self) -> None:
        module = _load_module()
        generator = FakeGenerator(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            private_path = Path(temp_dir) / "private.json"
            blind_path = Path(temp_dir) / "blind.json"
            public_path = Path(temp_dir) / "public.json"
            report = module.run_diagnostic(
                self._spec(module),
                _corpus_document(),
                corpus_sha256=module.EXPECTED_CORPUS_SHA256,
                personas_dir=ROOT / "personas",
                work_dir=Path(temp_dir) / "runtime",
                private_output=private_path,
                blind_output=blind_path,
                public_output=public_path,
                generate=generator,
            )
            private_report = json.loads(private_path.read_text(encoding="utf-8"))
            blind_report = json.loads(blind_path.read_text(encoding="utf-8"))
            public_report = json.loads(public_path.read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["api_call_count"], 20)
        self.assertEqual(len(generator.requests), 20)
        self.assertEqual(len(generator.requests[-1][1]), 40)
        self.assertEqual(generator.requests[-1][1][1].content, "离线用户输入 1")
        self.assertFalse(
            any(
                "离线参考回答" in message.content
                for _, messages in generator.requests
                for message in messages
            )
        )
        self.assertEqual(report["frozen_native_baseline"]["score"], 10)
        self.assertFalse(report["frozen_native_baseline"]["rerun"])
        self.assertEqual(report["runtime"]["turn_count"], 20)
        self.assertEqual(report["runtime"]["turn_index"], 20)
        self.assertEqual(report["runtime"]["raw_event_count"], 40)
        self.assertEqual(report["runtime"]["assistant_event_count"], 20)
        self.assertTrue(report["runtime"]["relationship_state_preserved"])
        self.assertTrue(report["runtime"]["turn_1_present_at_turn_20"])
        self.assertEqual(report["runtime"]["usage_totals"]["total_tokens"], 4300)
        self.assertNotIn("private_text", json.dumps(public_report, ensure_ascii=False))
        self.assertNotIn("离线用户输入", json.dumps(public_report, ensure_ascii=False))
        self.assertEqual(
            private_report["runtime"]["turns"][0]["user_prompt"],
            "离线用户输入 1",
        )
        self.assertEqual(set(blind_report["trajectories"]), {"MODEL_X"})
        blind_text = json.dumps(blind_report, ensure_ascii=False)
        for forbidden in ("glm-4.7-flash", "zhipu", "runtime", "native"):
            self.assertNotIn(forbidden, blind_text.lower())

    def test_corpus_fingerprint_is_a_hard_single_variable_gate(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "corpus fingerprint"):
                module.run_diagnostic(
                    self._spec(module),
                    _corpus_document(),
                    corpus_sha256="b" * 64,
                    personas_dir=ROOT / "personas",
                    work_dir=Path(temp_dir) / "runtime",
                    private_output=Path(temp_dir) / "private.json",
                    blind_output=Path(temp_dir) / "blind.json",
                    public_output=Path(temp_dir) / "public.json",
                    generate=FakeGenerator(module),
                )

    def test_exact_model_and_thinking_contract_fail_closed(self) -> None:
        module = _load_module()
        wrong_model = module.ProviderSpec(
            key="glm_47_flash",
            provider="zhipu",
            model="glm-other",
            endpoint="https://example.test/chat/completions",
            api_key="test-key",
            thinking_mode="disabled",
        )
        with self.assertRaisesRegex(ValueError, "exact model"):
            module.GLMRuntimeProvider(wrong_model)

        provider = module.GLMRuntimeProvider(self._spec(module), generate=FakeGenerator(module))
        with self.assertRaisesRegex(module.DiagnosticProviderError, "thinking"):
            provider.generate(
                [module.ProviderMessage(role="user", content="hello")],
                thinking=True,
            )


if __name__ == "__main__":
    unittest.main()
