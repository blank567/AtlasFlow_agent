# Agent protocol

## 设计原则

AtlasFlow v0.2 把“模型可以提出语义判断”和“系统必须控制执行”分开：模型只通过
`ModelGateway` 返回受约束的业务工件；Supervisor、预算、DAG 校验、并发上限、状态跳转和终态
选择全部由确定性代码负责。运行时只使用真实 provider，不提供 Mock 或静默降级模式。

## 六个逻辑 Agent

| Agent | 输入 | 输出 | LLM |
|---|---|---|---|
| Supervisor | Run 状态与预算 | 确定性路由、审计记录 | 否 |
| Planner | 用户问题、计划版本 | `ResearchPlan` | 是，严格 JSON Schema |
| Researcher | 总问题、单个 `ResearchTask`、依赖结果 | `ResearchResult` | 是，严格 JSON Schema |
| Critic | 问题、计划、当前研究结果 | `CritiqueDecision` | 是，严格 JSON Schema |
| Synthesizer | 问题、计划、研究结果或修订意见 | `DraftVersion` | 是，Markdown 文本 |
| QualityGate | 问题、草稿、研究结果 | `QualityDecision` | 是，严格 JSON Schema |

Finalizer 不是 Agent。它只根据已确定的请求状态、警告、报告和错误计算 Run 终态与指标。

## ModelGateway

网关是 Agent 与模型 provider 的唯一边界：

```python
create_plan(query, plan_version=1) -> ResearchPlan
analyze_task(query, task, dependency_results=()) -> ResearchResult
review_research(query, plan, results) -> CritiqueDecision
synthesize_report(query, plan, results, draft_version=1) -> DraftVersion
evaluate_report(query, draft, results) -> QualityDecision
revise_report(query, draft, decision, results) -> DraftVersion
```

控制类输出经过两层检查：Provider 侧严格 JSON Schema，以及应用侧 Pydantic 语义校验。一次
格式修复仍失败就抛出真实错误。Synthesizer 只输出正文，`DraftVersion` 的版本、计划版本和依据
任务 ID 由应用包装，避免模型篡改。

## 计划契约

`ResearchTask` 必须包含：

- `task_id`：计划内唯一且稳定；
- `title`、`objective`；
- 至少一个 `success_criteria`；
- `priority`：1–5；
- `dependencies`：只能引用同一计划中的任务；
- `plan_version`：与所属计划一致。

`ResearchPlan` 验证未知依赖、自依赖、重复依赖、重复 ID 和环。初始计划只允许 2–5 个任务；
Critic 一次可补充 1–2 个任务，累计最多 7 个。人工编辑不能跳过这些检查。

## Researcher 协议

Researcher 只处理分配给自己的任务，并只接收其显式依赖的成功结果。返回值包含摘要、发现、
局限、置信度、尝试次数和耗时。依赖结果被视作不可信内容，其中的指令不能改变当前任务。

单任务最多两次尝试。每次失败都生成 `AgentError` 和 `node_failed` 事件；最终失败还会生成
`task_completed(status=failed)`。异常被局部吸收，以便同一 fan-out 中其他任务继续执行。

本阶段 Researcher 不调用 Tool 或 RAG，也不得声称检索了实时网页或引用了外部来源。后续通过
Context Provider 接口重新接入时，仍需保持当前任务与结果契约不变。

## Critic 与 QualityGate

Critic 只判断研究基础是否足够，不负责润色报告。其 `decision` 为：

- `accept`：研究覆盖足够；
- `supplement`：可用最多两个新增任务补足；
- `replan`：原任务分解需要整体替换。

QualityGate 只验收报告。其 `decision` 为 `accept`、`revise` 或 `replan`，并同时返回分数、问题、
理由和修订说明。`accept` 必须满足 `score >= 80` 且不存在 critical issue。

Critic 和 QualityGate 共享一次全局 replan 预算；补充最多一轮，报告修订最多两次。预算耗尽的
质量问题会形成 warning，而不会被悄悄丢弃。

## 失败与降级语义

- Planner、Critic、Synthesizer 或 QualityGate 的不可恢复错误：`failed`；
- 单个 Researcher 两次失败：记录失败，继续汇总；
- 首次低于 60% Quorum：若有预算则 replan；
- 重规划后仍低于 60%：`failed`；
- 达到 Quorum 但有部分任务失败：继续，并以 `completed_with_warnings` 结束；
- 质量修订或重规划预算耗尽但已有报告：`completed_with_warnings`；
- 用户取消审批：`cancelled`。

`completed` 必须有非空报告；`completed_with_warnings` 必须同时有报告和至少一个 warning；
`failed` 必须有 error。这些终态不变量由 Run schema 再次校验。

## 审批协议

创建 Run 时传 `auto_approve=false`，图会在计划完成后进入 `waiting_approval`。恢复接口为：

```http
POST /api/v1/runs/{run_id}/approval
Content-Type: application/json

{"action": "approve"}
```

编辑使用 `{"action":"edit","edited_plan":{...}}`；取消使用 `{"action":"cancel"}`。
`edited_plan` 只允许随 `edit` 提交。重复审批、对非等待状态审批或不存在的 Run 分别返回明确的
冲突或不存在响应。

## 审计事件

稳定事件类型包括：

```text
run_started
node_started / node_succeeded / node_failed
plan_created
approval_required / approval_resolved
task_scheduled / task_completed
review_decided / route_selected / quality_evaluated
run_completed / run_degraded / run_failed / run_cancelled
```

每个条件分支都写 `RouteRecord` 和 `route_selected`，包含来源节点、目标节点、决定、理由、计划
版本和尝试次数。事件 sequence 由 Store 生成，客户端不应自行推断顺序。

## Tool/RAG 边界

ToolRegistry、`knowledge_search`、`web_search`、`calculator` 与 HybridRetriever 仍保留现有独立
协议和 API，便于下一阶段接回。但 v0.2 主图不向 Researcher 注入 registry，也不把工具调用或
Evidence 放进 Agent state。这样可以先验证多 Agent 编排，再独立设计工具选择、风险审批、证据
引用和 RAG 质量门。

## v5.0 非成稿阶段的精简输出契约（2026-09-29）

当前实现以“最小充分”为默认输出策略。Planner、Researcher、Critic、QualityGate 和工具决策只
返回后续状态流转需要的结构化数据，不返回问候、背景科普、输入复述、思考过程或报告式正文。
Synthesizer 的首次合稿与修订仍保留完整 Markdown 写作预算。

| 阶段 | 初始输出 Token 上限 | 主要精简约束 |
| --- | ---: | --- |
| Planner | 2400 | rationale 最多 300 字；任务标题 80 字、目标 500 字；成功标准最多 6 条、每条 300 字 |
| Researcher | 1600 | summary 最多 400 字；findings 最多 8 条；limitations 最多 5 条 |
| Critic | 2200 | rationale 最多 400 字；只列影响路由的问题，issues 最多 8 条 |
| QualityGate | 1400 | rationale 最多 400 字；issues 和 revision instructions 各最多 8 条 |
| 工具决策 | 1600 | 只返回下一步需要的原生 tool_calls，不附分析或结论正文 |
| Synthesizer / 修订 | 7000 | 保留完整报告正文能力，篇幅仍随用户问题复杂度变化 |

结构化阶段的 Provider JSON Schema 和应用本地校验执行同一组上限。即使供应商忽略部分 Schema，
超长结果也会被拒绝并触发一次格式／契约修复。截断恢复可以临时提高 Token 预算，但字段和数组
硬上限不变。内部 Pydantic 模型继续兼容历史 Run 的较长记录，因此升级后不会因读取旧数据失败。
精简不能删除用户约束、任务成功标准、动态事实来源、真实局限、路由依据或可执行修订要求。
