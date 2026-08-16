from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from companion_mind.providers import DeepSeekConfig, DeepSeekProvider


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deepseek_live_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("deepseek_live_smoke", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load smoke script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post_json(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls)
        return {
            "id": f"smoke-{index}",
            "choices": [
                {
                    "message": {
                        "content": f"仅供离线测试的响应 {index}",
                        "reasoning_content": "synthetic" if index == 2 else None,
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 100 + index,
                "completion_tokens": 10 + index,
                "total_tokens": 110 + index * 2,
                "ignored_text": "must not enter report",
            },
        }


class DeepSeekLiveSmokeTests(unittest.TestCase):
    def test_report_proves_runtime_contract_without_logging_content(self) -> None:
        smoke = _load_smoke_module()
        transport = FakeTransport()
        provider = DeepSeekProvider(
            DeepSeekConfig(api_key="synthetic-test-key"),
            transport=transport,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            report = smoke.run_smoke(
                provider,
                personas_dir=ROOT / "personas",
                work_dir=Path(temp_dir),
            )

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["persona_id"], "LIN-ZHIYAO")
        self.assertEqual(report["turn_index"], 2)
        self.assertEqual(report["raw_event_count"], 4)
        self.assertEqual(report["assistant_event_count"], 2)
        self.assertFalse(report["content_logged"])
        self.assertEqual([item["thinking"] for item in report["modes"]], [False, True])
        self.assertNotIn("ignored_text", report["modes"][0]["usage"])
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("仅供离线测试的响应", serialized)
        self.assertNotIn("synthetic-test-key", serialized)
        self.assertEqual(
            [call["payload"]["thinking"]["type"] for call in transport.calls],
            ["disabled", "enabled"],
        )


if __name__ == "__main__":
    unittest.main()
