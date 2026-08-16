from __future__ import annotations

import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from companion_mind.providers import ProviderResponse


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deepseek_private_baseline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("deepseek_private_baseline", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load private baseline script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeProvider:
    name = "deepseek"
    model = "deepseek-v4-flash"

    def __init__(self) -> None:
        self.turn = 0

    def generate(self, messages, *, thinking=False):
        self.turn += 1
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            content=f"连续关系中的离线响应 {self.turn}",
            response_id=f"private-{self.turn}",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "ignored_text": "must stay private",
            },
        )


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


class DeepSeekPrivateBaselineTests(unittest.TestCase):
    def test_loader_requires_exact_sequential_twenty_pairs(self) -> None:
        module = _load_module()
        raw = json.dumps(_corpus_document(), ensure_ascii=False).encode("utf-8")
        corpus, fingerprint = module.load_corpus(base64.b64encode(raw).decode())
        self.assertEqual(len(corpus["pairs"]), 20)
        self.assertEqual(len(fingerprint), 64)

        invalid = _corpus_document()
        invalid["pairs"].pop()
        encoded = base64.b64encode(json.dumps(invalid).encode()).decode()
        with self.assertRaisesRegex(ValueError, "exactly 20"):
            module.load_corpus(encoded)

    def test_loader_rejects_missing_or_invalid_secret(self) -> None:
        module = _load_module()
        with self.assertRaisesRegex(ValueError, "is required"):
            module.load_corpus("")
        with self.assertRaisesRegex(ValueError, "valid base64"):
            module.load_corpus("not base64!")

    def test_public_report_excludes_content_and_private_report_keeps_it(self) -> None:
        module = _load_module()
        corpus = _corpus_document()
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as temp_dir:
            private_output = Path(temp_dir) / "private-result.json"
            report = module.run_baseline(
                provider,
                corpus,
                corpus_sha256="a" * 64,
                personas_dir=ROOT / "personas",
                work_dir=Path(temp_dir) / "runtime",
                private_output=private_output,
            )
            private_report = json.loads(private_output.read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["turn_index"], 20)
        self.assertEqual(report["raw_event_count"], 40)
        self.assertEqual(report["assistant_event_count"], 20)
        self.assertTrue(report["relationship_state_preserved"])
        self.assertFalse(report["content_logged"])
        self.assertEqual(report["semantic_score"], "PENDING_PRIVATE_REVIEW")
        self.assertEqual(report["usage_totals"]["total_tokens"], 300)
        self.assertNotIn("ignored_text", report["usage_totals"])
        public_text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("离线用户输入", public_text)
        self.assertNotIn("离线参考回答", public_text)
        self.assertNotIn("连续关系中的离线响应", public_text)
        self.assertEqual(private_report["turns"][0]["user_prompt"], "离线用户输入 1")
        self.assertEqual(
            private_report["turns"][-1]["generated_response"],
            "连续关系中的离线响应 20",
        )


if __name__ == "__main__":
    unittest.main()

