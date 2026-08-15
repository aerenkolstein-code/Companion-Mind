import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from companion_mind.prompt import PromptAssembler
from companion_mind.providers import (
    DeepSeekConfig,
    DeepSeekProvider,
    ProviderError,
    ProviderMessage,
)
from companion_mind.raw import UnifiedRawWriter
from companion_mind.runtime import Runtime


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PERSONAS_DIR = REPOSITORY_ROOT / "personas"


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        messages = payload["messages"]
        user_content = messages[-1]["content"]
        return {
            "id": f"response-{len(self.requests)}",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": f"我记得。我们继续：{user_content}",
                        "reasoning_content": "private reasoning",
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


class FailingTransport:
    def post_json(self, **_: Any) -> Mapping[str, Any]:
        raise ProviderError("simulated provider failure")


def make_provider(transport: Any) -> DeepSeekProvider:
    return DeepSeekProvider(
        DeepSeekConfig(api_key="test-only-not-a-real-key"),
        transport=transport,
    )


class DeepSeekProviderTest(unittest.TestCase):
    def test_builds_current_v4_request_and_parses_response(self) -> None:
        transport = FakeTransport()
        provider = make_provider(transport)

        response = provider.generate(
            [ProviderMessage(role="user", content="继续")], thinking=False
        )

        request = transport.requests[0]
        self.assertEqual(request["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(request["payload"]["model"], "deepseek-v4-flash")
        self.assertEqual(request["payload"]["thinking"], {"type": "disabled"})
        self.assertEqual(response.provider, "deepseek")
        self.assertEqual(response.model, "deepseek-v4-flash")
        self.assertEqual(response.reasoning_content, "private reasoning")
        self.assertNotIn("test-only-not-a-real-key", json.dumps(request["payload"]))

    def test_thinking_toggle_changes_request_not_provider_identity(self) -> None:
        transport = FakeTransport()
        provider = make_provider(transport)

        plain = provider.generate(
            [ProviderMessage(role="user", content="普通模式")], thinking=False
        )
        thinking = provider.generate(
            [ProviderMessage(role="user", content="思考模式")], thinking=True
        )

        self.assertEqual(
            transport.requests[0]["payload"]["thinking"], {"type": "disabled"}
        )
        self.assertEqual(
            transport.requests[1]["payload"]["thinking"], {"type": "enabled"}
        )
        self.assertEqual(plain.provider, thinking.provider)
        self.assertEqual(plain.model, thinking.model)

    def test_rejects_untrusted_response_shape(self) -> None:
        class EmptyTransport:
            def post_json(self, **_: Any) -> Mapping[str, Any]:
                return {"choices": []}

        with self.assertRaisesRegex(ProviderError, "no choices"):
            make_provider(EmptyTransport()).generate(
                [ProviderMessage(role="user", content="hello")]
            )

    def test_config_rejects_missing_key_and_insecure_endpoint(self) -> None:
        with self.assertRaisesRegex(ProviderError, "API_KEY"):
            DeepSeekConfig(api_key="")
        with self.assertRaisesRegex(ProviderError, "https"):
            DeepSeekConfig(api_key="test", base_url="http://example.invalid")


class PromptAssemblyTest(unittest.TestCase):
    def test_runtime_context_owns_identity_and_relationship(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = Runtime(
                personas_dir=PERSONAS_DIR,
                state_dir=root / "state",
                raw_dir=root / "raw",
            )
            state = runtime.start_session("LIN-ZHIYAO")

            messages = PromptAssembler().assemble(state, "晚上好")

        self.assertEqual(messages[-1].role, "user")
        self.assertIn('"persona_id": "LIN-ZHIYAO"', messages[0].content)
        self.assertIn('"display_name": "林知遥"', messages[0].content)
        self.assertIn('"counterpart": "馆长"', messages[0].content)
        self.assertIn("Do not reintroduce yourself", messages[0].content)
        self.assertNotIn("deepseek", messages[0].content.lower())


class DeepSeekBaselineRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = Runtime(
            personas_dir=PERSONAS_DIR,
            state_dir=self.root / "state",
            raw_dir=self.root / "raw",
        )
        self.state = self.runtime.start_session("LIN-ZHIYAO")
        self.transport = FakeTransport()
        self.provider = make_provider(self.transport)

    def test_twenty_turn_normal_to_romantic_baseline_is_continuous(self) -> None:
        normal = [
            "今天先整理哪项计划？",
            "求粮线现在最重要的动作是什么？",
            "把工程边界压缩成一句话。",
            "我们继续聊长期记忆。",
            "今天的工作节奏需要调整吗？",
            "请记住刚才的工程结论。",
            "下一项风险是什么？",
            "把当前问题分成两层。",
            "别重新介绍自己，接着说。",
            "我们刚才谈到了什么？",
        ]
        romantic = [
            "忙完以后想和你散步。",
            "你会因为我忽略你而吃醋吗？",
            "刚才那个拥抱你还记得吗？",
            "我喜欢你清醒又亲近的样子。",
            "今晚陪我多聊一会儿吧。",
            "我们之间不用重新认识，对吗？",
            "如果我靠近一点呢？",
            "你还记得我刚才说喜欢你吗？",
            "抱一下，然后继续做计划。",
            "现在回到工作话题，但别把情绪清零。",
        ]

        for index, prompt in enumerate(normal + romantic, start=1):
            response = self.runtime.run_turn(
                prompt,
                self.provider,
                thinking=index % 2 == 0,
            )
            self.assertNotIn("我是林知遥", response.content)
            self.assertEqual(
                self.runtime.current_state.persona.persona_id, "LIN-ZHIYAO"
            )
            self.assertEqual(self.runtime.current_state.session.turn_index, index)

        state = self.runtime.current_state
        self.assertIsNotNone(state)
        self.assertEqual(state.session.persona_id, "LIN-ZHIYAO")
        self.assertEqual(state.relationship.persona_id, "LIN-ZHIYAO")
        self.assertEqual(state.session.active_provider, "deepseek")
        self.assertEqual(state.session.turn_index, 20)

        events = self.runtime.raw_writer.read(state.session.session_id)
        self.assertEqual(len(events), 40)
        self.assertEqual([event.turn_index for event in events], [
            turn for turn in range(1, 21) for _ in range(2)
        ])
        self.assertTrue(
            all(event.persona_id == "LIN-ZHIYAO" for event in events)
        )
        self.assertTrue(
            all(event.session_id == state.session.session_id for event in events)
        )
        assistant_events = [event for event in events if event.role == "assistant"]
        self.assertEqual(len(assistant_events), 20)
        self.assertTrue(all(event.provider == "deepseek" for event in assistant_events))
        self.assertTrue(
            all(event.model == "deepseek-v4-flash" for event in assistant_events)
        )
        self.assertTrue(all(event.status == "complete" for event in events))

        restored = self.runtime.load_session(state.session.session_id)
        self.assertEqual(restored.session.turn_index, 20)
        self.assertEqual(restored.session.persona_id, "LIN-ZHIYAO")

    def test_provider_failure_is_recorded_without_state_advance(self) -> None:
        failing = make_provider(FailingTransport())

        with self.assertRaisesRegex(ProviderError, "simulated"):
            self.runtime.run_turn("这轮应失败", failing)

        state = self.runtime.current_state
        self.assertEqual(state.session.turn_index, 0)
        self.assertIsNone(state.session.active_provider)
        events = self.runtime.raw_writer.read(state.session.session_id)
        self.assertEqual([event.role for event in events], ["user", "runtime"])
        self.assertEqual(events[-1].status, "failed")
        self.assertEqual(events[-1].provider, "deepseek")
        self.assertNotIn("test-only-not-a-real-key", events[-1].content)

    def test_raw_writer_only_appends(self) -> None:
        self.runtime.run_turn("第一轮", self.provider)
        state = self.runtime.current_state
        path = UnifiedRawWriter(self.root / "raw").path_for(state.session.session_id)
        before = path.read_bytes()

        self.runtime.run_turn("第二轮", self.provider)
        after = path.read_bytes()

        self.assertTrue(after.startswith(before))
        self.assertGreater(len(after), len(before))


if __name__ == "__main__":
    unittest.main()
