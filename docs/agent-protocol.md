# Agent and tool protocol

## 核心规则

Agent 不直接拼装 HTTP 请求。模型能力经 `ModelGateway` 访问，业务数据与外部能力经具名工具
访问；`ToolRegistry` 在执行工具前验证参数与权限，并统一记录结果。运行时 provider 均为真实
OpenRouter 集成，不存在 Mock 或静默降级。

## Agent 职责

| Agent | 职责 | 是否调用真实 LLM / 工具 |
|---|---|---|
| Supervisor | 初始化 run state，协调执行 | 否 |
| Planner | 将用户问题拆成研究步骤 | OpenRouter LLM，JSON Schema 输出 |
| Researcher | 收集内部知识与当前 Web 证据 | `knowledge_search`、`web_search` |
| Writer | 基于计划和证据生成带引用的 Markdown 草稿 | OpenRouter LLM |
| Critic | 检查事实依据、引用和结构 | OpenRouter LLM，JSON Schema 输出 |
| Reporter | 输出草稿并附加未解决的质量问题 | 否 |

Researcher 当前采用确定性的工具策略：每轮先调用 `knowledge_search`，若 registry 中存在
`web_search` 再调用它。Critic 返回问题且仍有迭代预算时，问题会拼入查询并重新执行检索、
写作和审查。证据按 `source_id` 去重。

## 工具契约

每个工具声明：

- 稳定的 `name` 和人类可读的 `description`；
- Pydantic `arguments_model`；
- 风险等级：`low`、`medium` 或 `high`；
- 异步 `run` 实现；
- 结构化 `ToolResult`。

`ToolResult` 包含业务数据、`Evidence` 列表、执行耗时、错误和是否可重试。`Evidence` 包含
`source_id`、标题、原文片段、URI、最终分数和检索元数据。工具调用会另外保存
`ToolCallRecord`，其中包括参数、成功状态、耗时、证据 ID 和错误。

## 当前工具

### `knowledge_search`

参数为 `query` 与 `top_k`。它调用 HybridRetriever，按“BM25 + OpenRouter Embedding +
RRF + OpenRouter Rerank”生成证据。最终 evidence score 是 reranker 分数，其他阶段分数保存在
metadata 中。

### `web_search`

参数为 `query` 与 `max_results`。它通过 Chat Completions 请求
`openrouter:web_search` server tool，并限制单次搜索使用次数、结果数和返回字符数。只有响应中
包含 URL citation annotations 时才返回成功；没有引用时返回失败结果，不伪造网页或摘要来源。

### `calculator`

参数为 `expression`。它用受限 AST 解释器执行基础算术，禁止函数调用、变量访问和任意代码
执行。工具已注册，当前 Researcher 的固定策略不会自动调用它，可通过 registry 单独演示。

## 执行中间件

ToolRegistry 当前实现：

1. 按名称查找工具；
2. 检查 `ToolContext.approved_risks`；
3. 用 Pydantic 校验参数；
4. 在 `TOOL_TIMEOUT_SECONDS` 内执行；
5. 对超时和连接错误做有限指数退避重试；
6. 写入耗时并生成 LangSmith tool span（仅在 tracing 开启时上传）。

OpenRouter client 还有独立的 provider 级重试，用于网络异常、408、429 和 5xx。不要把两层
重试解释为无限重试；它们都受 `MAX_TOOL_RETRIES` 限制。Planner/Critic 的结构化输出额外设置
`require_parameters`、启用 `response-healing`，并最多进行一次格式级重试。

## 授权策略

- Low：只读检索和确定性计算；当前 Researcher 仅获此权限。
- Medium：SQL 查询、Python 执行或高成本操作。
- High：外部写入、发送消息、购买或破坏性操作。

ToolRegistry 会拒绝不在 context 授权集合中的工具。当前仓库没有自动批准中高风险操作；新增
此类工具时应增加显式 approval node，并将用户授权写入审计记录。

## 失败语义

- 参数无效或风险未授权：调用被拒绝，不执行工具；
- OpenRouter 认证、区域、模型可用性或配额错误：真实失败，不回退到本地伪结果；
- Web Search 没有 URL citation：工具标记失败，不能作为可信网页证据；
- Embedding 数量、维度或 Rerank 索引异常：检索立即失败，避免带错误分数继续写作；
- 工作流异常：RunService 将 run 标为 `failed`，API/SSE 暴露可诊断状态。
