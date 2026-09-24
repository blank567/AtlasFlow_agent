# AtlasFlow

AtlasFlow 是一个面向面试展示的、可观测且可评测的多 Agent 研究平台。它用 LangGraph 编排
`Supervisor → Planner → Researcher → Writer → Critic → Reporter`，通过统一工具注册表调用内部
RAG、实时网页搜索和安全计算器，并可把整条执行链路发送到 LangSmith。

当前版本：`v0.1.0`（首个真实 Provider 版本）。**运行时不提供 Mock 或离线降级**：LLM、
Embedding、Rerank 和 Web Search 都会调用真实 OpenRouter API；配置缺失或服务失败时，任务会
明确失败并保留错误状态，不会用伪造结果冒充成功。自动化测试通过依赖注入使用测试替身，因此
测试不会消耗真实额度，也不会进入生产装配路径。

## 为什么它适合作为面试项目

- **多 Agent 不是简单串行脚本**：LangGraph 包含共享状态、条件边、Critic 返工闭环和最大迭代限制。
- **工具调用可治理**：统一完成参数验证、风险授权、超时、重试、执行审计和 Evidence 标准化。
- **RAG 链路完整**：BM25 + 真实向量检索 → RRF 融合 → 真实 Reranker → 可解释分数。
- **Evidence-first**：报告只消费结构化证据，保留来源 URI、Chunk、各阶段分数和引用编号。
- **真实网页检索**：Researcher 使用 OpenRouter `openrouter:web_search` server tool 获取 URL 引用。
- **质量闭环**：Critic 检查回答覆盖度、引用合法性和证据夸大，必要时回到 Researcher 补充证据。
- **可观测**：Agent、模型、工具和工作流形成嵌套 LangSmith Trace；正文上传默认关闭。
- **可演示**：FastAPI + SSE 实时推送执行事件，Next.js 页面展示轨迹、工具、证据和最终报告。
- **可测试、可评测**：离线单测不依赖网络，在线回归集必须显式传入 `--live` 才会真实调用 API。

## 系统架构

```text
┌──────────────────── Next.js UI ────────────────────┐
│ 任务输入 │ 实时 Agent 轨迹 │ Tool Calls │ Evidence │ 报告 │
└────────────────────────┬───────────────────────────┘
                         │ REST + SSE
┌────────────────────────▼───────────────────────────┐
│                      FastAPI                       │
│ Document API │ Run API │ Event Stream │ Health API │
└────────────────────────┬───────────────────────────┘
                         │
┌────────────────────────▼───────────────────────────┐
│                  LangGraph Workflow                │
│ Supervisor → Planner → Researcher → Writer → Critic│
│                           ↑                 │       │
│                           └──── revise ─────┘       │
│                                      ↓             │
│                                   Reporter         │
└───────────────┬──────────────────────┬──────────────┘
                │                      │
       OpenRouter ModelGateway     ToolRegistry
                                       │
                   ┌───────────────────┼──────────────────┐
                   ▼                   ▼                  ▼
             Knowledge Search      Web Search        Calculator
                   │           openrouter:web_search       │
                   ▼
  BM25 + OpenRouter Embedding → RRF → OpenRouter Rerank
```

更细的职责边界见 [架构文档](docs/architecture.md)，状态和工具协议见
[Agent/工具协议](docs/agent-protocol.md)。

## 一次请求的完整流程

1. `POST /api/v1/runs` 创建任务，`RunService` 将状态从 `pending` 改为 `running`。
2. `Supervisor` 初始化共享状态和事件流。
3. `Planner` 调用真实 LLM，并用 JSON Schema 约束输出 3–5 个研究步骤。
4. `Researcher` 只能通过 `ToolRegistry` 调用工具：
   - `knowledge_search` 执行混合 RAG；
   - `web_search` 调用 OpenRouter 网页搜索并提取 URL citations；
   - `calculator` 提供受限算术能力，当前研究流不默认调用它。
5. `Writer` 把检索内容当作不可信数据，只根据 Evidence 生成带 `[S1]` 引用的 Markdown 报告。
6. `Critic` 通过结构化输出检查报告。存在问题且未达到迭代上限时回到 `Researcher`。
7. `Reporter` 输出最终报告及仍未解决的质量备注。
8. `RunService` 持久化为 `completed` 或 `failed`；SSE 向前端推送每个生命周期和 Agent 事件。

核心共享状态：

```text
run_id, query, plan, evidence, tool_calls,
draft, critiques, iteration, needs_revision, report
```

## 工具调用与安全边界

```text
Agent Tool Request
  → ToolRegistry 按名称路由
  → Pydantic 校验 arguments
  → ToolContext 检查风险授权
  → asyncio timeout
  → 对可重试错误指数退避
  → LangSmith 子 Trace
  → ToolResult(success, data, evidence, error, duration_ms)
```

| 工具 | 风险 | 真实能力 |
|---|---:|---|
| `knowledge_search` | low | OpenRouter Embedding + BM25 + RRF + OpenRouter Rerank |
| `web_search` | low | OpenRouter server-side web search，返回真实 URL citations |
| `calculator` | low | 受限 AST 算术解释器，不使用 `eval` |

工具分为 `low / medium / high`。当前工作流仅授权低风险工具；未来的写数据库、发邮件、执行代码等
能力需要显式授权或 Human-in-the-loop 节点。详细规则见
[Agent/工具协议](docs/agent-protocol.md)。

## RAG 流程

### 文档进入系统

```text
Document
  → whitespace normalization
  → overlap chunking (420 chars / 80 overlap)
  → tokenization
  → in-memory chunk store
  → 首次查询时批量调用真实 document embedding
```

### 查询

```text
Query
  ├─ BM25 lexical score
  └─ OpenRouter query embedding + cosine similarity
                    ↓
        Reciprocal Rank Fusion (RRF)
                    ↓
          OpenRouter real reranker
                    ↓
 Evidence(content, URI, lexical/vector/fusion/rerank scores)
```

Embedding 采用 document/query 两种 `input_type`。`.env.example` 默认模型
`nvidia/nemotron-3-embed-1b:free` 返回 2048 维向量。pgvector 的 HNSW `vector` 索引最多支持
2000 维，因此 `infra/init.sql` 使用 `HALFVEC(2048)` 和 `halfvec_cosine_ops`；切换模型时必须同步
迁移数据库维度。

当前 Run、Event 和 Chunk 仍在内存中；PostgreSQL/pgvector 脚本展示下一阶段的持久化结构，尚未接入
默认 Repository。

## Provider 配置

项目根目录的 `.env` 是唯一的本地密钥入口，已被 `.gitignore` 排除。不要把 Key 写进源码、README、
前端环境变量或日志。

首次配置：

```powershell
Copy-Item .env.example .env
```

如果 `.env` 已经存在，不要执行上面的覆盖命令。关键字段如下：

```dotenv
LLM_PROVIDER=openrouter
LLM_MODEL=openrouter/free
LLM_API_KEY=your_openrouter_key
LLM_BASE_URL=https://openrouter.ai/api/v1

EMBEDDING_PROVIDER=openrouter
EMBEDDING_MODEL=nvidia/nemotron-3-embed-1b:free
EMBEDDING_API_KEY=your_openrouter_key
EMBEDDING_BASE_URL=https://openrouter.ai/api/v1

RERANK_PROVIDER=openrouter
RERANK_MODEL=nvidia/llama-nemotron-rerank-vl-1b-v2:free
RERANK_API_KEY=your_openrouter_key
RERANK_BASE_URL=https://openrouter.ai/api/v1

SEARCH_PROVIDER=openrouter
SEARCH_MODEL=
SEARCH_API_KEY=
SEARCH_BASE_URL=https://openrouter.ai/api/v1
```

- `SEARCH_MODEL` 留空时复用 `LLM_MODEL`；`SEARCH_API_KEY` 留空时复用 `LLM_API_KEY`。
- 所有 `*_BASE_URL` 填 API 根地址即可；代码也会把误填的 `/chat/completions`、`/embeddings`、
  `/rerank` 后缀标准化为根地址。
- `openrouter/free` 会在可用的免费模型间路由，适合演示但结果和延迟可能变化。正式评测建议固定
  一个支持 JSON Schema 的模型。
- Planner 和 Critic 的结构化请求设置 `provider.require_parameters=true`，只路由到支持 JSON
  Schema 的端点，并启用 OpenRouter `response-healing` 修复偶发 JSON 语法错误；应用层仍会做
  一次受限重试和字段校验。
- 免费模型不等于所有功能都免费；网页搜索可能单独产生费用。运行在线脚本前检查 OpenRouter 余额、
  数据策略和限额。
- 示例中的免费 NVIDIA 模型页面提示不要提交敏感或机密数据。真实业务应换成符合数据合规要求的
  Provider/模型。

参考：[OpenRouter Quickstart](https://openrouter.ai/docs/quickstart)、
[Embeddings API](https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings)、
[Rerank API](https://openrouter.ai/docs/api/api-reference/rerank/create-rerank)、
[Structured Outputs](https://openrouter.ai/docs/guides/features/structured-outputs)、
[Web Search](https://openrouter.ai/docs/guides/features/server-tools/web-search) 和
[pgvector 索引限制](https://github.com/pgvector/pgvector#hnsw)。

## LangSmith

在 `.env` 中填写：

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your_langsmith_key
LANGSMITH_PROJECT=atlasflow-dev
LANGSMITH_TRACE_CONTENT=false
```

默认 `LANGSMITH_TRACE_CONTENT=false`：仍记录 Trace 层级、耗时和执行关系，但输入正文、Evidence 和
输出正文会被标记为已脱敏。只在确定数据可上传时改为 `true`。

Trace 层级示例：

```text
workflow.research-run
├── agent.supervisor
├── agent.planner
│   └── model.openrouter.plan
├── agent.researcher
│   ├── tool-registry.execute (knowledge_search)
│   └── tool-registry.execute (web_search)
├── agent.writer
│   └── model.openrouter.draft
├── agent.critic
│   └── model.openrouter.critique
└── agent.reporter
```

## 快速启动

### 1. 使用已有 `langchain` Conda 环境

在项目根目录执行：

```powershell
conda activate langchain
python --version
python -m pip install -e ".[dev]"
```

项目要求 Python 3.11+。当前验证环境是 `E:\conda_envs\langchain\python.exe`（Python 3.11）。

### 2. 启动后端

确保 `.env` 中的真实 Provider 配置完整，然后执行：

```powershell
conda activate langchain
uvicorn atlasflow.main:create_app --factory --app-dir backend/src --reload
```

- Health：<http://localhost:8000/api/v1/health>
- Swagger：<http://localhost:8000/docs>
- 如果 Key 或模型缺失，应用会在启动阶段明确报错；这是预期的 fail-fast 行为。

### 3. 启动前端

另开一个终端：

```powershell
Set-Location frontend
npm install
npm run dev
```

打开 <http://localhost:3000>。前端默认请求 `http://localhost:8000/api/v1`，也可通过
`NEXT_PUBLIC_API_URL` 修改。

### 4. 可选基础设施

```powershell
docker compose up -d
```

这会启动 PostgreSQL/pgvector 与 Redis，但 v0.1 默认仍使用内存 Store。

## API 示例

创建研究任务：

```bash
curl -X POST http://localhost:8000/api/v1/runs \
  -H "Content-Type: application/json" \
  -d '{"query":"AtlasFlow 如何通过工具调用和 RAG 提升结果可信度？"}'
```

查询任务：

```bash
curl http://localhost:8000/api/v1/runs/{run_id}
```

订阅 SSE 事件：

```bash
curl -N http://localhost:8000/api/v1/runs/{run_id}/events
```

写入知识库（Embedding 在首次检索时生成）：

```bash
curl -X POST http://localhost:8000/api/v1/documents \
  -H "Content-Type: application/json" \
  -d '{"title":"产品资料","content":"待检索的正文内容"}'
```

列出工具：

```bash
curl http://localhost:8000/api/v1/tools
```

## 验证、测试和评测

### 离线自动化检查

这些命令不会访问外部 Provider：

```powershell
conda activate langchain
pytest
ruff check .

Set-Location frontend
npm run build
```

测试替身仅通过 `ProviderBundle` 注入测试容器；默认 `build_container(Settings())` 始终创建真实
OpenRouter Provider，不存在运行时假数据回退。

### 真实 Provider 冒烟测试

以下命令会联网，也可能产生费用：

```powershell
conda activate langchain
$env:PYTHONPATH="backend/src"

# 分别验证 LLM / Embedding / Rerank / Web Search
python scripts/live_provider_smoke.py

# 验证完整 Agent 图：Planner → RAG/Tools → Writer → Critic → Reporter
python scripts/live_workflow_smoke.py
```

脚本只输出状态、数量和维度，不打印 Key、Evidence 正文或报告正文。

### 在线回归集

```powershell
conda activate langchain
$env:PYTHONPATH="backend/src"
python evals/run_local.py --live
```

`--live` 是防误触确认；不传时脚本拒绝执行。当前指标检查任务是否成功、要求的工具是否被调用、
预期知识库来源是否命中。完整方案见 [评测文档](docs/evaluation.md)。

## 项目结构

```text
atlasflow/
├── backend/src/atlasflow/
│   ├── agents/          # LangGraph 工作流与真实 ModelGateway
│   ├── api/             # FastAPI 路由与 SSE
│   ├── providers/       # OpenRouter chat/embedding/rerank client
│   ├── rag/             # Chunk、BM25、Vector、RRF、Rerank
│   ├── tools/           # 工具协议、注册表与真实内置工具
│   ├── bootstrap.py     # 生产依赖装配与 Demo 知识
│   ├── config.py        # .env 配置
│   ├── observability.py # LangSmith Trace 与内容脱敏
│   ├── schemas.py       # Run、Evidence、ToolCall 协议
│   └── service.py       # Run 生命周期
├── backend/tests/       # 离线测试与测试专用替身
├── frontend/            # Next.js 实时演示页面
├── docs/                # 架构、协议、评测和演示脚本
├── evals/               # 在线回归数据集与 Runner
├── scripts/             # 真实 Provider / 完整工作流冒烟脚本
├── infra/init.sql       # PostgreSQL + pgvector halfvec 结构
├── .env.example         # 无密钥的真实 Provider 配置模板
├── docker-compose.yml
└── pyproject.toml
```

## 推荐面试演示

使用问题：

> AtlasFlow 如何通过工具调用和 RAG 提升多 Agent 结果可信度？

建议讲解顺序：

1. 在 UI 发起任务，展示 Planner 的结构化计划。
2. 展示 `knowledge_search` 和 `web_search` 两类真实工具调用及耗时。
3. 打开 Evidence，说明 BM25、向量、RRF、Rerank 四层分数。
4. 展示 Writer 的 `[S1]` 引用和来源 URL。
5. 展示 Critic 是否触发返工，以及 LangSmith 中的嵌套 Trace。
6. 最后说明错误路径、内容脱敏、在线评测确认开关和当前持久化边界。

完整口述脚本见 [Demo Run](docs/demo-run.md)。

## 常见问题

### 启动时报 `API key is required`

确认命令在项目根目录执行，且 `.env` 对应的 Key 已填写。Embedding 和 Rerank 默认需要各自 Key；
Search 可以复用 LLM Key。

### 模型返回 403 或区域不可用

这是 Provider/模型的区域或账户限制，不会触发本地回退。将 `.env` 中的模型改成当前账户可访问、
且支持所需能力的 OpenRouter model slug，再执行真实冒烟测试。

### Planner/Critic 返回 JSON 错误

选择支持 `response_format: json_schema` 的 LLM。`openrouter/free` 的底层模型会变化；需要稳定回归时
固定具体模型。

### pgvector 报向量维度不匹配

默认 SQL 是 2048 维 `halfvec`，只匹配示例 Embedding 模型。更换模型后迁移
`knowledge_chunks.embedding` 的维度并重建 HNSW 索引。

## 当前边界与路线图

当前边界：

- Run、Event、知识 Chunk 存于内存，进程重启后消失。
- 文档解析目前接收纯文本，尚未包含 PDF/Word/Markdown loader。
- 后台任务依赖 FastAPI 进程，尚不支持跨进程恢复。
- `openrouter/free` 路由和网页搜索使延迟、可用性与费用受外部服务影响。
- Calculator 仅支持基础算术；通用 Python/SQL 工具必须先加入隔离沙箱。

下一步：

- PostgreSQL Run/Event Repository 与 pgvector Hybrid Retriever。
- Redis 队列、幂等键、断点恢复和 worker 横向扩容。
- PDF/Word/Markdown 解析、异步索引和内容级去重。
- Human-in-the-loop 审批、容器化代码沙箱和更细粒度 RBAC。
- LangSmith Dataset、成本/延迟指标、Prompt 版本和 PII 策略。
- 多租户、限流、配额、OpenTelemetry/Prometheus/Grafana 与报告导出。

---

## v0.2.0 — Agent 编排重构（2026-09-20）

> 本节按版本追加，前面的 v0.1 内容作为历史记录保留。若旧说明与本节冲突，以 v0.2 为准。

### 本版本目标

v0.2 先稳定 Agent 之间的逻辑、协议、并发和质量闭环，暂不把 Tool/RAG 接回研究主图。这样可以
单独验证“计划是否可执行、分支是否有界、失败是否可解释、人工是否能接管”。运行时仍没有 Mock
模式；离线测试使用依赖注入的 test doubles，生产容器只创建真实 OpenRouter provider。

这是一次有意的 Schema 升级，不保留 v0.1 Run 字段兼容层。`/tools`、`/documents` 和现有
Tool/RAG 实现继续保留，但它们不是 v0.2 Agent 工作流的隐含依赖。

### 六个逻辑 Agent

| Agent | 责任 | 输入/输出 | 是否调用 LLM |
|---|---|---|---|
| Supervisor | 初始化、预算控制、确定性路由、审计 | Run state / `RouteRecord` | 否 |
| Planner | 把问题拆成 2–5 个任务的 DAG | `ResearchPlan` | 是，严格 JSON Schema |
| Researcher Pool | 按拓扑波次并行完成单个研究任务 | `ResearchTask` / `ResearchResult` | 是，严格 JSON Schema |
| Critic | 在写报告前检查覆盖、冲突和缺口 | `CritiqueDecision` | 是，严格 JSON Schema |
| Synthesizer | 生成或修订完整 Markdown 报告 | `DraftVersion` | 是，纯文本 |
| QualityGate | 对报告打分并选择验收、修订或重规划 | `QualityDecision` | 是，严格 JSON Schema |

Finalizer 只负责确定性地计算终态、报告和指标，不算第七个 Agent。

### 完整执行流程

```text
START
  → Supervisor
  → Planner（2-5 个任务，校验 ID / 依赖 / 版本 / 无环）
  → Approval
      ├─ auto_approve=true：自动通过
      └─ auto_approve=false：interrupt，等待 approve / edit / cancel
  → Schedule Wave
  → Researcher Pool（LangGraph Send 动态 fan-out，最大并发 3）
  → Fan-in → Research Gate（成功率至少 60%）
  → Critic
      ├─ accept → Synthesizer
      ├─ supplement → 最多补 2 个任务，再研究一次
      └─ replan → Planner
  → Synthesizer
  → QualityGate
      ├─ accept（分数至少 80 且无 critical issue）→ Finalizer
      ├─ revise → Synthesizer
      └─ replan → Planner
  → END
```

Researcher 任务按依赖分波执行。无依赖任务可以真正并行；每个分支异常会被转换成结构化
`AgentError`，不会取消同一波的其他任务。单任务最多尝试 2 次，峰值并发和成功/失败数写入
`ExecutionMetrics`。

### 硬预算与终态语义

| 预算 | 默认上限 |
|---|---:|
| 初始研究任务 | 5（最少 2） |
| Researcher 并发 | 3 |
| 单任务尝试 | 2 |
| 研究 Quorum | 60% |
| 补充研究 | 1 轮，每轮最多 2 个任务 |
| 单计划累计任务 | 7 |
| 全局重规划 | 1 次 |
| 报告修订 | 2 次 |
| QualityGate 验收分 | 80 |

- 达到 Quorum 且全部成功、质量通过：`completed`；
- 达到 Quorum 但有部分任务失败，或质量预算耗尽且已有报告：
  `completed_with_warnings`；
- 首次低于 Quorum：使用一次全局 replan；第二版仍低于 Quorum：`failed`；
- Planner、Critic、Synthesizer、QualityGate 的不可恢复错误：`failed`；
- 用户在审批节点取消：`cancelled`。

所有循环都有硬上限，不会无限“反思”。成功终态必须有报告，降级终态必须有报告和 warning，
失败终态必须有 error，Run schema 会再次检查这些不变量。

### 新增 Agent 契约与 ModelGateway

`backend/src/atlasflow/agents/contracts.py` 新增：

- `ResearchTask`、`ResearchPlan`：版本化 DAG；
- `ResearchResult`：摘要、发现、局限、置信度、attempt、耗时；
- `CritiqueDecision`、`QualityDecision`：受约束的语义路由；
- `DraftVersion`：报告版本与依据任务；
- `AgentError`、`RouteRecord`、`ExecutionMetrics`：失败、路由和运行指标。

`ModelGateway` 现在只有六个稳定方法：

```python
create_plan(...)
analyze_task(...)
review_research(...)
synthesize_report(...)
evaluate_report(...)
revise_report(...)
```

计划、研究、Critic 和 QualityGate 输出使用严格 JSON Schema，并在解析/契约失败时只做一次格式
重试；生成与修订报告使用纯文本。全部方法保留 LangSmith trace。Researcher 会把依赖结果视为
不可信输入，当前阶段不声称执行过网页检索或 RAG。

### Human-in-the-loop

创建需要审批的 Run：

```bash
curl -X POST http://localhost:8000/api/v1/runs \
  -H "Content-Type: application/json" \
  -d '{"query":"评估多 Agent 系统架构","auto_approve":false}'
```

图会使用 LangGraph `interrupt()` 和内存 checkpointer 停在 `waiting_approval`。继续执行：

```bash
curl -X POST http://localhost:8000/api/v1/runs/{run_id}/approval \
  -H "Content-Type: application/json" \
  -d '{"action":"approve"}'
```

也可提交 `edit` 和完整 `edited_plan`；编辑后的 ID、依赖和无环性会重新校验。`cancel` 会进入
`cancelled`。当前恢复仅在同一进程内有效，持久恢复留到数据库阶段。

### Run、事件与 SSE

Run 状态扩展为：

```text
pending / running / waiting_approval /
completed / completed_with_warnings / failed / cancelled
```

审计事件包含连续 `sequence`、`event_id`、`run_id`、`event_type`、Agent、节点、task ID、计划
版本、attempt、状态、耗时、决策原因、时间和扩展数据。重要事件包括计划创建、审批、任务调度与
完成、Critic 决策、条件路由、质量评分和四类终态。

RunStore 在同一把锁内写入最终工件、状态与终态事件。SSE 会先按 sequence 发送所有事件，最后
只发送一次 `done`，修复了 v0.1 的终态竞态。

### 前端变化

演示页保留原视觉风格，并新增：

- 自动批准开关；
- 计划 DAG、依赖、优先级和各 Researcher 状态；
- 最新 Critic 路由、QualityGate 分数、模型调用和峰值并发；
- `approve` / JSON 编辑 / `cancel` 审批面板；
- `completed_with_warnings`、失败原因和 warning 展示；
- 全部 v0.2 SSE 事件类型。

### 配置项

`.env.example` 新增以下编排预算；默认值就是上表中的硬上限：

```dotenv
MAX_RESEARCH_TASKS=5
MAX_RESEARCH_CONCURRENCY=3
MAX_RESEARCH_ATTEMPTS=2
RESEARCH_QUORUM_RATIO=0.6
MAX_SUPPLEMENT_ROUNDS=1
MAX_SUPPLEMENT_TASKS=2
MAX_TASKS_PER_PLAN=7
MAX_REPLANS=1
MAX_REPORT_REVISIONS=2
QUALITY_THRESHOLD=80
```

LangSmith 仍通过 `LANGSMITH_TRACING`、`LANGSMITH_API_KEY`、`LANGSMITH_PROJECT` 控制；默认
`LANGSMITH_TRACE_CONTENT=false`，避免意外上传查询、任务结果和报告正文。

### v0.2 验证结果

本版本使用 `E:\conda_envs\langchain\python.exe` 完成离线验证：

```powershell
$env:TEMP='E:\codex\tmp'
$env:TMP='E:\codex\tmp'

python -m ruff check .
python -m pytest -q

Set-Location frontend
npm run build
```

结果：Ruff 通过；33 个后端测试通过；Next.js 生产构建通过。测试覆盖直接通过、真实并发上限、
重试、Quorum 降级/失败、Critic supplement/replan、QualityGate revise/replan、预算耗尽、核心
Agent 故障、人工审批三种操作、SSE 顺序与完整 API 生命周期。没有执行真实联网冒烟，避免在未
确认费用时消耗 Provider 额度。

### 下一版本顺序

1. PostgreSQL Run/Event Repository 与持久 LangGraph checkpointer；
2. 定义 Context Provider，在不破坏 Agent 契约的前提下重新接入 RAG；
3. 引入模型驱动但受策略约束的 Tool selection、权限与审批；
4. Evidence/Citation 契约、检索与引用质量门；
5. LangSmith Dataset、成本/延迟基线和真实在线回归。

---

## v0.3.0 — 结构化运行策略与审查上下文（2026-09-23）

> 本节只追加 v0.3 的增量说明；前面的 v0.1/v0.2 内容作为历史记录保留。若旧说明与本节冲突，
> 以本节为准。

### 本版本解决的问题

过去如果把“第一次 Planner 只生成 2 个任务，第一次 Critic 再追加 1 个任务”写进 query，第二次
Critic 只会看到当前计划已有 3 个任务，却不知道第三个任务来自上一轮合法 supplement。它可能把
“初始 2 个任务”的要求重复应用到当前 active plan，错误选择 replan。

v0.3 把职责拆开：

- query 只描述研究内容，例如“黄山风景介绍”；
- `RunPolicy` 保存初始任务数、必需补充轮次、每轮补充数量和 replan 门槛；
- `PlanLineage` 分别保存初始计划与 Critic 的补充增量；
- `ReviewContext` 由 Workflow 生成，明确当前是第几轮、任务来源、版本/修订和剩余预算；
- `CritiqueDecision.validate_for()` 在 LLM 输出通过 JSON 校验后，再按工作流事实做语义校验。

部署配置仍是硬上限，单次 RunPolicy 只能在上限内收紧行为。当前策略字段为：

| 字段 | 范围/默认值 | 作用 |
|---|---|---|
| `initial_task_count` | `null` 或 2–5 | 指定 v1 初始计划的精确任务数 |
| `required_supplement_rounds` | 0–1，默认 0 | 在 accept/replan 前必须完成的 supplement 轮次 |
| `supplement_task_count` | `null` 或 1–2 | 指定一次 supplement 的精确新增任务数 |
| `replan_requires_critical_issue` | 默认 `true` | Critic replan 必须至少包含一个 critical issue |

### API 示例：黄山 2 → 3 → Accept

流程约束不再写进 query，而是作为 `policy` 单独提交：

```bash
curl -X POST http://localhost:8000/api/v1/runs \
  -H "Content-Type: application/json" \
  -d '{
    "query": "黄山风景介绍",
    "auto_approve": true,
    "policy": {
      "initial_task_count": 2,
      "required_supplement_rounds": 1,
      "supplement_task_count": 1,
      "replan_requires_critical_issue": true
    }
  }'
```

该策略对应的预期路径是：

```text
Planner
  → v1.r0：base_plan = [T1, T2]
  → 执行 T1、T2
  → Critic #1：ReviewContext 当前/预期任务数 = 2/2
  → required_supplement_rounds 尚未满足，只允许 supplement
  → SupplementBatch #1 = [T3]
  → v1.r1：active_plan = [T1, T2, T3]
  → 只执行新增的 T3，不重复执行 T1、T2
  → Critic #2：ReviewContext 当前/预期任务数 = 3/3，来源 = 初始 2 + 补充 1
  → accept
  → Synthesizer → QualityGate → Finalizer
```

这里的 `plan_version` 与 `plan_revision` 含义不同：

- supplement 是同一计划的追加修订：`v1.r0 → v1.r1`；
- replan 才会丢弃当前 active plan 并创建新版本：`v1.* → v2.r0`；
- v1 的补充历史保留在 v1 lineage 中，不会变成 v2 的 revision；
- API 终态会返回 `policy`、`plan_lineage`、`plan_lineages` 和每轮 `review_contexts`，便于审计。

### Replan 语义

Critic 的 replan 现在受以下边界约束：

1. 必须提供 `ReplanReason` 枚举值：`invalid_decomposition`、
   `unresolved_critical_gap`、`irreconcilable_conflict` 或 `dependency_dead_end`；
2. `replan_requires_critical_issue=true` 时，issues 中必须至少有一个 `critical`；
3. policy 要求的 supplement 尚未发生时，accept 和 replan 都会被拒绝；
4. ReviewContext 的计划版本、补充预算、任务 ID、当前数量和预期数量必须自洽；
5. Critic Prompt 明确规定：当 `current_task_count == expected_task_count` 时，任务总数、任务数增加或
   “首版只能有 2 个任务”本身都不能成为 replan 理由。

OpenRouter Gateway 会对格式或上述契约校验失败的结构化输出做一次修复重试。例如第二轮把合法的
`2 + 1` 错报为任务超量、却只给出 warning issue 的 replan，会因缺少 critical issue 被拒绝并要求
模型重新决策。若修复后仍无效，Workflow 进入可审计的失败路径，不会执行该 replan。

### 本版本修改文件

生产后端：

- `backend/src/atlasflow/agents/contracts.py`：RunPolicy、SupplementBatch、PlanLineage、
  ReviewContext、ReplanReason 和 Critic 语义校验；
- `backend/src/atlasflow/agents/gateway.py`：Planner policy、Critic context、严格 Schema 和修复重试；
- `backend/src/atlasflow/agents/workflow.py`：policy 预算校验、lineage/context 状态及 supplement/replan
  生命周期；
- `backend/src/atlasflow/schemas.py`：创建请求和 RunRecord 暴露 policy、lineage 与 review contexts；
- `backend/src/atlasflow/service.py`、`backend/src/atlasflow/api/routes.py`：policy 全链路透传与工件持久化。

测试与文档：

- `backend/tests/fakes.py`：记录 policy/context/审查计划的确定性 Gateway；
- `backend/tests/test_agent_contracts.py`：lineage 与无 critical issue replan 契约测试；
- `backend/tests/test_openrouter.py`：精确 Planner Schema 和第二轮错误 replan 修复测试；
- `backend/tests/test_workflow.py`：2 → 3 → Accept、不重复研究、跨版本 lineage 和 replan 语义测试；
- `backend/tests/test_api.py`：policy 从 HTTP 请求传播到终态工件的集成测试；
- `README.md`、`docs/code-reference.md`：v0.3 使用说明与后端代码参考。

### 验证结果

离线套件共 38 个测试函数，参数化展开后为 40 个用例，全部通过。新增的确定性断言包括：

- 初始计划恰好 2 个任务，补充后 active plan 恰好 3 个任务；
- Critic 决策历史严格为 `[supplement, accept]`，`replan_count == 0`；
- T1、T2、T3 各执行一次，补充研究不会重跑初始任务；
- 第二轮 context 是 `v1.r1`，并明确记录 2 个初始 IDs 和 1 个补充 ID；
- 无 critical issue 的 replan 不会路由回 Planner；
- 真正 replan 后得到独立的 `v2.r0`，不会继承 v1 的 supplement revision；
- API 请求中的 policy 与终态返回的 policy、lineage、review contexts 一致。

这些测试全部使用依赖注入的 FakeModelGateway，不访问网络，也不改变生产环境仍使用真实 OpenRouter
Provider 的边界。

---

## v0.4.0 — 持久运行时、LangSmith 与可视化控制台（2026-09-24）

> 本节只追加 v0.4 的增量说明；v0.1–v0.3 保留为演进记录。若历史说明与本节冲突，以本节为准。
> 本版本聚焦 Agent 执行、运行时和可观测性，没有继续修改 Tool/RAG。

### 本版本交付

v0.4 把原来的单页演示升级为可持续使用的本地 Agent 控制台：

```text
Next.js Console
  ├── Workbench：创建 Run、选择结构化 RunPolicy
  ├── Runs：历史、筛选、分页、rerun、确认删除
  ├── Run Detail：SSE、Agent Flow、Task DAG、审批、报告、Trace
  ├── Analytics：本地 Run/Event 聚合指标
  └── Settings：数据库、Provider、LangSmith 安全状态
             │
             ▼
FastAPI → RunService → ResearchWorkflow / LangGraph
             │                 │
             │                 ├── initial / approval_resume Trace Segment
             │                 └── OpenRouter 真实 usage metadata
             ▼
SQLite：runs + run_projections + run_events
```

前端保持浅色、简洁、中文业务文案与英文技术名词并用。桌面端展示完整图和指标，窄屏自动简化布局。

### 五个页面

| 路由 | 作用 |
|---|---|
| `/` | 创建研究 Run，使用策略预设或精确编辑 `RunPolicy`，查看最近记录 |
| `/runs` | 按研究问题/Run ID、状态、日期筛选，分页、rerun、删除终态 Run |
| `/runs/[runId]` | 动态查看 Agent Flow、Task DAG、PlanLineage、事件、审查、报告和 Trace |
| `/analytics` | 展示状态分布、耗时趋势、决策、计划版本和本地汇总指标 |
| `/settings` | 只读展示安全配置状态，并主动检测 LangSmith 连通性 |

Run 详情通过 `@xyflow/react` 展示 Agent 与 Task 图。节点可点击，抽屉会展示输入、结果、耗时、
依赖、成功标准、置信度、错误和审查信息。图状态来自 SSE 事件，即使终态 RunRecord 尚未完成落库，
也能动态呈现计划和任务进度。研究报告使用禁止原始 HTML 的 Markdown 渲染。

### SQLite Run/Event 数据库

默认数据库为 `<repo>/data/atlasflow.sqlite3`；本项目位于 E 盘时，所有数据库、WAL/SHM 和构建产物
也都留在 E 盘。可在 `.env` 中用 `DATABASE_PATH` 覆盖路径。

数据库职责：

- `runs`：Run 身份、query、来源 Run 和创建时间；
- `run_projections`：经 Pydantic 校验的最新 `RunRecord` 快照与筛选字段；
- `run_events`：按 `(run_id, sequence)` 排序的 append-only 审计日志；
- 状态迁移、最终工件和终态事件原子提交；
- `event_id` 去重、外键级联删除、WAL、busy timeout 和单进程异步锁；
- 启动时把未完成 Run 标记为 `failed/backend_restart`，不会假装可恢复。

当前仍使用 LangGraph `MemorySaver`，所以数据库可以恢复历史和审计数据，但不能在进程重启后继续
执行中断点。运行时明确限定为本地单用户、单后端进程；不要用多个 Uvicorn worker 同时写该库。

### SSE 快照校准

`GET /api/v1/runs/{run_id}/events` 支持：

- 连续 sequence、`id:` 和命名事件；
- `Last-Event-ID` 请求头或 `last_event_id` query 参数断点续传；
- heartbeat 注释、服务端 retry 提示和终态 `done`；
- 前端按 `event_id/sequence` 去重、指数退避重连，并用 Run 快照校准；
- 终态后延迟刷新，接收可能稍晚解析完成的 LangSmith Trace URL。

### LangSmith 接入与隐私边界

一个 AtlasFlow Run 可包含多个 Trace Segment：首次调用为 `initial`，人工审批恢复为
`approval_resume`。每个 Segment 都用 `atlasflow_run_id`、segment ID、kind、tag 和 metadata 关联，
因此既能在本地 Run 详情聚合，也能跳转到 LangSmith 深入排查。

默认配置仍是：

```dotenv
LANGSMITH_TRACING=false
LANGSMITH_TRACE_CONTENT=false
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=atlasflow-dev
```

- API key 只在后端环境中读取，设置接口和前端永不返回它；
- `LANGSMITH_TRACE_CONTENT=false` 时隐藏 trace 输入/输出，并对嵌套 secret、token、authorization 等
  字段做递归脱敏；
- 初始化、上传、连通检查或 URL 解析失败时进入降级状态，不阻断 Agent 主流程；
- 前端只展示 `disabled / not_configured / ready / unreachable` 等安全状态；
- 删除本地 Run 只删除 SQLite 数据，不会删除 LangSmith 远端 Trace。

### 真实 Provider 指标

OpenRouter chat 请求显式请求 usage。每次调用只保存 Provider 实际返回或本地可直接测量的元数据：

- requested model 与实际 model；
- request ID、generation ID、HTTP 状态、重试次数；
- prompt/completion/total tokens；
- Provider 返回的 cost 原始数值；
- 调用耗时、成功/失败和错误类型。

缺失值保持“不可用”，不会估算 token、成本或伪造调用记录。指标挂在对应 Trace Segment 下，前端
可查看单次调用和 Run 汇总。

### v0.4 API 增量

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/v1/runs` | query/Run ID、状态、带时区日期筛选与分页 |
| `POST` | `/api/v1/runs/{run_id}/rerun` | 复制原 query、policy、审批方式，生成新 Run |
| `POST` | `/api/v1/runs/{run_id}/cancel` | 取消 pending/running/waiting_approval Run |
| `DELETE` | `/api/v1/runs/{run_id}` | 输入完整 Run ID 后删除终态本地记录 |
| `GET` | `/api/v1/analytics?range=7d\|30d\|all` | 读取 SQLite 聚合指标 |
| `GET` | `/api/v1/settings/status` | 返回无 secret 的数据库、Provider、LangSmith 状态 |
| `POST` | `/api/v1/settings/langsmith/check` | 主动检查 LangSmith 连通性 |
| `GET` | `/api/v1/runs/{run_id}/events` | 可恢复的 SSE 事件流 |

原有创建 Run、读取 Run 和结构化审批接口保持兼容。删除只允许终态 Run；活动 Run 必须先取消，
并且请求体必须提供与路径完全相同的 `confirmation_run_id`。

### 使用既有 langchain 环境运行

后端继续使用用户指定的 `E:\conda_envs\langchain`：

```powershell
$env:TEMP='E:\codex\tmp'
$env:TMP='E:\codex\tmp'
$env:PIP_CACHE_DIR=(Join-Path (Get-Location) '.pip-cache')

& 'E:\conda_envs\langchain\python.exe' -m pip install -e .
& 'E:\conda_envs\langchain\python.exe' -m uvicorn atlasflow.main:create_app --factory --reload --port 8000
```

另开终端启动前端：

```powershell
Set-Location frontend
npm install
npm run dev
```

`frontend/.npmrc` 把 npm cache 固定到仓库根目录 `.npm-cache`；pip cache 也由上面的命令固定在
仓库根目录 `.pip-cache`。两者均已忽略，不会提交，也不会占用 C 盘。

前端默认连接 `http://localhost:8000/api/v1`，可用 `NEXT_PUBLIC_API_URL` 覆盖。真实执行需要有效的
LLM 配置；LangSmith 是可选能力，即使未配置也可以运行 Agent。

### v0.4 验证结果

在 E 盘 langchain 环境完成：

- Ruff 全仓检查通过；
- 后端 `58 passed`，只有 Starlette 上游 anyio alias 弃用警告；
- 前端 `npm run typecheck` 通过；
- Next.js 生产构建通过，7/7 页面生成成功；
- `git diff --check` 用于最终检查空白错误。

测试全部使用 Fake Gateway/Mock HTTP transport，不消耗真实 Provider 额度；因此本次结果不代表用户
账号下的 OpenRouter 或 LangSmith 已完成在线调用。填写 `.env` 后，可从 Settings 页检查 LangSmith，
再创建一条真实 Run 验证远端 Trace 和 Provider usage。

### 明确未纳入 v0.4 的内容

- 没有把 Tool/RAG 重新接回 Agent 主图；
- 没有新增 RAG 向量数据库；SQLite 只保存 Run/Event，并非 RAG 数据库；
- 没有 LangSmith Dataset、Eval 或 Feedback；
- 没有多用户认证、多进程写入或持久 LangGraph checkpointer；
- 没有估算 Provider 成本或 token。
