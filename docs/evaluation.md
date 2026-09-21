# Evaluation plan

## 测试层次

### 1. 离线契约与工作流测试

`backend/tests` 通过依赖注入使用 test doubles，不读取 API key、不访问网络，也不是运行时模式。
场景化 `FakeModelGateway` 实现与生产网关相同的六方法协议，并记录并发和调用次数。

```powershell
conda activate langchain
python -m pytest
```

v0.2 的核心分支包括：

- 直接通过与终态不变量；
- 真实异步 fan-out，峰值并发不超过 3；
- Researcher 重试、达到 Quorum 的部分失败降级；
- 低于 Quorum 后重规划，第二次仍不足则失败；
- Critic 的 supplement、replan 与预算耗尽；
- QualityGate 的 revise、replan 与两次修订预算；
- Planner、Synthesizer、QualityGate 等核心故障；
- 人工 approve、edit、cancel；
- SSE sequence、终态事件顺序和唯一 `done`；
- 健康检查、保留的 Tool/Document API 与完整 Run API 生命周期。

### 2. 真实 Provider 冒烟测试

`scripts/live_workflow_smoke.py` 读取 `.env`，真实调用六个 Agent 路径，只输出状态、数量、质量分、
路由数、耗时与警告，不打印密钥、研究正文或报告正文：

```powershell
python scripts/live_workflow_smoke.py
```

`scripts/live_provider_smoke.py` 额外检查保留的 Embedding、Rerank 和 Web Search provider。两类
冒烟都会联网并可能产生费用；任何调用失败都如实失败，不启用 Mock fallback。

### 3. LangSmith 评测

设置 `LANGSMITH_TRACING=true`、`LANGSMITH_API_KEY` 和 `LANGSMITH_PROJECT` 后，可按 run 查看
Planner、Researcher、Critic、Synthesizer、QualityGate 与 provider span。默认的
`LANGSMITH_TRACE_CONTENT=false` 会隐藏正文；只有对评测数据完成脱敏后才应开启内容记录。

下一阶段将稳定样本沉淀到 LangSmith Dataset，对不同 prompt、模型和重新接入后的 RAG/Tool
策略做对比，而不是让在线结果进入离线单元测试。

## v0.2 指标

| 指标 | 计算方法 | 目标 |
|---|---|---|
| DAG 有效率 | 通过 ID、依赖、版本、无环校验的计划 / 全部计划 | 100% 进入执行图 |
| Research success ratio | 成功任务 / 当前计划任务 | 正常路径 ≥ 60% |
| Peak concurrency | 同时活跃 Researcher 数 | 1–3，绝不超过 3 |
| Retry exhaustion | 耗尽两次尝试的任务数 | 全部显式记录 |
| Route audit coverage | 条件路由是否有 `RouteRecord` 与事件 | 100% |
| Quality acceptance | score ≥ 80 且无 critical issue | 100% |
| Terminal invariant rate | 成功有报告、降级有警告、失败有错误 | 100% |
| SSE ordering | sequence 连续、终态事件后唯一 `done` | 100% |
| Completion/degraded/failure rate | 按固定数据集分组统计 | 建立基线后持续比较 |
| P95 latency / model calls | LangSmith 或 ExecutionMetrics | 按场景设基线 |

Tool 选择准确率、Retrieval hit rate、Citation coverage/correctness 暂不作为 v0.2 主图指标；它们
将在 RAG/Tool 重新接入时恢复，并与 Agent 编排指标分开报告。

## 回归流程

1. 从失败 Run 或 LangSmith trace 选择代表性样本；
2. 删除密钥、个人信息、机密查询和模型正文；
3. 标注预期计划特征、允许路由、终态和质量底线；
4. 先把路由与预算问题固化为离线场景测试；
5. 再显式运行真实 provider 冒烟或 Dataset 评测；
6. 比较正确性、延迟、模型调用次数与降级比例后决定是否合并。

## 结果解释

- 真实模型的任务拆分、Critic 路由和 QualityGate 分数会波动；预算与终态不变量不能波动。
- `completed_with_warnings` 是显式降级，不等同于完全成功，也不应在 UI 中隐藏。
- 一次分支异常不应取消同一波的其他 Researcher；低于 Quorum 才决定是否整体重规划。
- RunStore 和 checkpointer 当前都在内存中，进程重启不属于本阶段恢复测试范围。
- 第三方模型与 LangSmith trace 不应接收未脱敏的敏感数据。
