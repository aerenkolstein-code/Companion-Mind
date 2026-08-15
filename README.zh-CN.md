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

当前实测：**22/22 tests**、五个演示事件完整回放、live/replay snapshot 精确一致、MitigationSpec 已验证并留指纹。

当前边界：无模型 API、无事务数据库、不证明 AI 意识，也不接入私人档案；以上只证明当前公开夹具与本地原型的可执行集成。
