# Architecture

## 系统边界

AtlasFlow v0.1 将传输层、编排层、模型访问、工具执行、检索和存储拆成独立边界：

```text
Next.js UI
    │ HTTP + SSE
FastAPI API
    │
RunService ─────────────────── InMemoryRunStore
    │                               │ 事件 / 结果
LangGraph ResearchWorkflow ─────────┘
    ├── OpenRouterModelGateway
    │     └── OpenRouter Chat Completions
    └── ToolRegistry
          ├── KnowledgeSearchTool
          │     └── HybridRetriever
          │           ├── OpenRouter Embeddings
          │           └── OpenRouter Rerank
          ├── OpenRouterWebSearchTool
          │     └── OpenRouter `openrouter:web_search`
          └── CalculatorTool（本地受限 AST）
```

运行时没有 Mock provider，也不会在远程调用失败时回退到伪造结果。LLM、Embedding、
Rerank 和 Web Search 均使用真实 OpenRouter 请求；缺少密钥、模型名不合法或 provider
不是 `openrouter` 时，应用会给出明确的配置错误。测试通过依赖注入使用 test doubles，
它们不属于运行模式。

## Provider 边界

`build_openrouter_providers` 分别构造 LLM、Embedding、Rerank 和 Search client：

- `LLM_*` 驱动 Planner、Writer 和 Critic；
- `EMBEDDING_*` 生成文档向量与查询向量；
- `RERANK_*` 对融合后的候选文档重新排序；
- `SEARCH_*` 驱动 OpenRouter Web Search server tool。`SEARCH_API_KEY` 为空时复用
  `LLM_API_KEY`，`SEARCH_MODEL` 和 `SEARCH_BASE_URL` 为空时也分别继承 LLM 配置。

四类 provider 的超时由 `PROVIDER_TIMEOUT_SECONDS` 控制。OpenRouter client 对网络异常、
HTTP 408、429 和 5xx 做有限次数重试；401/403 等配置或权限错误会直接失败。错误信息经过
截断和密钥脱敏，API key 不进入 Agent state。Planner/Critic 的 JSON Schema 请求设置
`provider.require_parameters=true` 并启用 `response-healing`；应用层还会对非 JSON 响应做
一次受限重试，重试耗尽后如实失败。

## Agent 状态与图

LangGraph state 只携带业务数据：

```text
run_id, query, plan, evidence, tool_calls,
draft, critiques, iteration, needs_revision, report
```

执行图如下：

```text
START
  → Supervisor
  → Planner
  → Researcher ──→ ToolRegistry ──→ RAG / Web tools
  → Writer
  → Critic
      ├── 有问题且仍有重试预算 → Researcher
      └── 通过或预算耗尽 → Reporter
  → END
```

Supervisor 初始化状态；Planner、Writer 和 Critic 通过真实 LLM 完成计划、写作和质量检查；
Researcher 每轮通过 ToolRegistry 调用知识库检索与 Web Search；Reporter 确定性地整理最终
Markdown。Critic 的问题会加入下一轮检索查询，默认最多执行两轮质量检查。

## RAG 数据流

当前检索器将文档和 chunk 保存在进程内，但检索计算不是占位实现：

1. 文档按 `420` 字符、`80` 字符重叠切块；
2. 首次检索时批量调用真实 Embedding API，为尚未向量化的 chunk 生成
   `search_document` 向量；
3. 每次查询生成 `search_query` 向量；
4. 分别计算 BM25 与向量余弦相似度排名；
5. 用 Reciprocal Rank Fusion（RRF）合并两路排名，取 `top_k * 3` 个候选；
6. 调用真实 Rerank API，返回最终 `top_k` 条证据。

每条知识库证据保留 `document_id`、`chunk_index`、BM25 分数、向量分数、RRF 分数和
rerank 分数，便于解释与评测。进程重启后，上传文档和已计算向量都会丢失；持久化属于后续
扩展，不应把当前实现描述为生产级向量库。

## 工具与安全边界

`ToolRegistry` 负责工具注册、Pydantic 参数校验、风险授权、超时、有限重试、耗时记录和
LangSmith tool span。当前工作流只授予 `low` 风险：

- `knowledge_search` 返回内部知识库的可追溯 chunk；
- `web_search` 必须从 OpenRouter 响应中解析到 URL citation 才算成功，不会生成假来源；
- `calculator` 使用受限 AST，只允许基础算术。它已注册，但当前 Researcher 不会自动选择它。

中高风险工具仍需要显式扩展 `ToolContext.approved_risks`，未来应再配合人工审批节点。

## 可观测性

LangSmith 是可选能力。`LANGSMITH_TRACING=false` 时不发送 trace；启用后可观察 workflow、
Agent、LLM 和 tool 的嵌套 span。`LANGSMITH_TRACE_CONTENT=false` 是默认值，此时输入与输出
正文会被结构化占位信息替代；只有显式设置为 `true` 时，查询、文档片段和模型输出才会进入
trace。无论是否上传正文，输入处理都会移除 `self`、API key 和 authorization 等敏感字段。
即便如此，也不应把机密或个人数据用于演示。

## 当前限制与演进方向

| 当前实现 | 后续生产化方向 |
|---|---|
| `InMemoryRunStore` | PostgreSQL run/event repository |
| 进程内 chunk、BM25 与向量 | PostgreSQL full-text search + pgvector / 专用向量库 |
| FastAPI 进程内后台任务 | Redis 队列或持久化工作流 worker |
| OpenRouter server-side Web Search | 可选的专用搜索、抓取、内容净化与缓存层 |
| 单进程 SSE 事件流 | 可恢复的消息流与跨实例事件分发 |
