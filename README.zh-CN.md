# Companion-Mind

**带可观察状态、来源链与可执行保护门的最小持续认知运行时。**

Portfolio Status：**CURRENT ARTIFACT** · Evidence Level：**E3——可运行、已在本地测试的原型**

长上下文检索解决“过去能否找回”；Companion-Mind 继续追问：“正确约束能否在下一次状态写入前到场？”

## 首个闭环

`CM-GUARD-001` 为 `EVAL-CASE-001` 实现 Closure Guard：父目标仍有 `OPEN`、`UNKNOWN`、等待、阻塞或待处理子任务时，拒绝把父目标写成 `DONE`。

配套 LLM Evaluation Lab 对五个 public-safe 变体做了确定性实测：

| 策略 | 准确率 | 过早关闭率 |
|---|---:|---:|
| 朴素基线 | 20% | 100% |
| `CM-GUARD-001` | 100% | 0% |

以上是公开测试夹具结果，不是生产性能或通用模型能力声明。

## Executable Integration v0.3

当前纵切保留 append-only JSONL、State/Agenda 重建与确定性 Replay，并新增 `mitigation-spec/v1` 加载。Evaluation Lab 产出的 JSON 规范经过版本、目标错误、Guard 类型、决策映射和状态集合校验后，才注册为 `CM-GUARD-001`；不支持或含糊的规范直接失败关闭。

```bash
python -m pip install -e .
python -m unittest discover -s tests -v

event_dir="$(mktemp -d)"
companion-mind demo --event-log "$event_dir/events.jsonl" > "$event_dir/live.json"
companion-mind replay --event-log "$event_dir/events.jsonl" > "$event_dir/replayed.json"
cmp "$event_dir/live.json" "$event_dir/replayed.json"
```

运行时 snapshot 会记录规范的 canonical SHA-256 fingerprint，供 Evaluation Lab 证明回归测试实际执行的是同一份配置。

当前实测：**40/40 tests**、五个演示事件完整回放、live/replay snapshot 精确一致、MitigationSpec 已验证并留指纹。

## LIN-ZHIYAO Runtime v0.2｜STEP-02

当前新增 Provider 中立接口、DeepSeek V4 Flash Adapter、由 Runtime State 生成的 Prompt Assembly、严格 Response Parsing，以及 append-only Unified RAW。公开 CI 使用合成对话和离线 Fake Transport 验证连续 20 轮 NORMAL → ROMANTIC 基线：`persona_id`、`session_id`、Session State 与 provider/model 来源保持连续。

真实调用通过环境变量 `DEEPSEEK_API_KEY` 配置，默认模型为 `deepseek-v4-flash`；密钥不会写入代码、状态或 RAW。当前不声称已完成真实 DeepSeek 人格效果验收，也不声称跨模型连续性。私人《初期撩骚》RAW 不进入仓库和公开 CI。

当前边界：已实现模型 API Adapter，但公开验收不调用付费模型；无 Grok、无跨模型 Handoff、无事务数据库、不证明 AI 意识，也不公开私人档案。以上只证明当前公开夹具与本地原型的可执行集成。
