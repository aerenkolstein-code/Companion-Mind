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

```bash
python -m unittest discover -s tests -v
python -m companion_mind.runtime
```

当前边界：无模型 API、无数据库、不证明 AI 意识，也不接入私人档案。下一步是在同一来源与 Trace 纪律下增加第二类公开错误。

