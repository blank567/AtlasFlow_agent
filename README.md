# AtlasFlow

当前版本：`v0.6.0`。最新变更见文末的 [v0.6.0](#v060)；
下方 v0.1–v0.4.5 章节保留为版本演进记录。历史描述与当前实现不一致时，以最新版本章节为准。

当前 Agent 主流程：Supervisor → Planner → Approval → Scheduler/Researcher → Research Gate →
Critic → Synthesizer → Quality Gate → Finalizer。推理 Agent 已接入统一工具阶段，RAG 尚未接回主流程；
FastAPI 控制台展示实时事件和运行记录，LangSmith 记录 Trace，Studio 是独立的图调试入口。

> 以下项目概述至 `v0.2.0` 章节之前属于 v0.1.0 的历史说明，不代表当前工作流。

AtlasFlow 是一个面向面试展示的、可观测且可评测的多 Agent 研究平台。它用 LangGraph 编排
`Supervisor → Planner → Researcher → Writer → Critic → Reporter`，通过统一工具注册表调用内部
RAG、实时网页搜索和安全计算器，并可把整条执行链路发送到 LangSmith。

v0.1.0 历史说明（首个真实 Provider 版本）。**运行时不提供 Mock 或离线降级**：LLM、
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

## v0.4.1 — 真实执行路径与 Trace 入口修复（2026-09-24）

本节只记录 v0.4.1 相对于 v0.4.0 的增量，不改写上面的历史版本说明。

- Agent 执行拓扑不再使用固定六节点直线。前端展示 11 个 LangGraph 节点，连线只从 `route_selected` 事件提取；未发生的路线不会伪装成已执行。Critic 的 `Supplement`、`Replan` 以及 Quality Gate 的 `Revise` 回边用不同颜色和标签展示，重复经过的边显示次数。若审查请求重规划但预算耗尽，实际走向 Synthesizer/Finalizer，则标成“决策改道”，不误标为已执行 Replan。
- 图下新增按事件序号排列的完整路由记录。点击节点、连线或记录可查看对应的 Plan 版本、决策和原因，因此 `2 → 3 → Accept`、补充任务后再重规划等多轮过程可逐步追溯。
- 前端保留全部 `route_selected` 事件；普通高频遥测仍只保留最近 1000 条。长运行不会因事件窗口截断而丢失早期路径。刷新快照也会与实时 SSE 事件合并，避免快照稍旧时刚发生的路由短暂消失。
- Trace 卡片同时提供“查看执行路径图”和“LangSmith 调用树”入口，并明确两者区别：LangSmith Trace 是父子 Run / Span 的执行树与耗时详情，不是 AtlasFlow 的条件路由拓扑。已检查远端真实 Trace 含根 Run、LangGraph 子 Run、节点和模型调用；若 LangSmith 页面仍为空白，应另查登录工作区、浏览器加载或远端权限，而非假定没有生成 Trace。
- 新增 `npm run test:flow`，用确定性事件覆盖 Supplement、Replan、Revise、重复经过、事件乱序及超过 1000 条普通事件时的路由保留；`npm run typecheck` 和 `npm run build` 仍用于前端验证。

验证结果：`npm run test:flow`、`npm run typecheck`、`npm run build`、Ruff 均通过；E 盘 langchain 环境的后端测试为 `58 passed`（仅 Starlette 上游弃用警告）。真实 LangSmith 页面在用户浏览器中的展示仍需登录同一工作区进行人工确认；本次只验证了远端 Trace 的父子 Run 数据和应用内执行路径。

## v0.4.2 — LangSmith 内容 Trace 与 LangGraph Studio（2026-09-24）

本节只追加 v0.4.2 的增量，不改写旧版说明。

### 为什么旧 Trace 的节点输入/输出为空

此前本地 `.env` 没有 `LANGSMITH_TRACE_CONTENT`，配置默认值为 `false`。后端据此设置 `LANGSMITH_HIDE_INPUTS=true` 与 `LANGSMITH_HIDE_OUTPUTS=true`，因此远端仍有嵌套 Run 和耗时，但不保留节点正文。这是有意的隐私默认值，不是模型没有传入数据。

本机的 `.env` 现已显式设为 `LANGSMITH_TRACE_CONTENT=true`。**重启 FastAPI 后创建的新 Run** 会把节点输入、输出及查询/任务/报告内容发送到 LangSmith；现有密钥字段和常见 token 格式仍经脱敏处理，但一般业务内容不会再隐藏。已经上传为空的旧 Trace 无法补录。仓库的 `.env.example` 继续默认 `false`，避免其他安装环境意外上传数据。若要恢复仅元数据模式，把本机 `.env` 改回 `false` 并重启后端。

### Studio 本地入口

- `langgraph.json` 注册 `atlasflow` 图，指向 `backend/src/atlasflow/studio.py` 的工厂函数；Studio 复用生产工作流的 11 个节点、条件边和真实 OpenRouter Gateway，增加 `studio_entry` 节点把 `query`、`auto_approve`、`policy` 转为完整状态。
- Studio 的 Agent Server 在 `127.0.0.1:2024` 独立运行，线程与检查点由 Studio 管理。它不会写入 FastAPI 的 SQLite Run/Event 数据库，也不会自动导入以前的 AtlasFlow Run；这是同一张图的另一种调试入口，而非原有控制台的数据库视图。
- `scripts/start-studio.ps1` 固定使用 `E:\conda_envs\langchain`，把临时文件与缓存指向 E 盘，并启用 Python UTF-8 模式，以避免 Windows GBK 读取 Studio OpenAPI 文件失败。

在仓库根目录的 PowerShell 终端执行：

```powershell
$env:TEMP='E:\codex\tmp'
$env:TMP='E:\codex\tmp'
$env:PIP_CACHE_DIR='E:\codex\agent\.pip-cache'
$env:UV_CACHE_DIR='E:\codex\agent\.uv-cache'
& 'E:\conda_envs\langchain\python.exe' -m pip install --no-user --no-build-isolation -e '.[studio]'
.\scripts\start-studio.ps1
```

打开 [LangGraph Studio](https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024)，选择 `atlasflow`，在 Graph 模式输入：

```json
{
  "query": "介绍黄山风景，先规划 2 个任务，审查后补充 1 个任务",
  "auto_approve": true,
  "policy": {
    "initial_task_count": 2,
    "required_supplement_rounds": 1,
    "supplement_task_count": 1
  }
}
```

Settings 页也有 Studio 快捷入口。Studio 的本地服务仅用于开发测试；运行真实任务会调用 OpenRouter 并产生费用。LangSmith Studio 是网页，但图的执行发生在本机 Agent Server；若连不上，应先检查 `http://127.0.0.1:2024/ok`、登录的 LangSmith 工作区，以及浏览器是否允许该网页访问本地服务。

验证：后端 `62 passed`、Ruff 和前端类型检查通过；Studio 本地服务的 `/ok`、图拓扑与输入 Schema 接口均返回成功，且假网关测试跑完一次完整 Studio 图。前端生产构建通过。若前端开发服务器正在运行，构建时可设置 `$env:ATLASFLOW_NEXT_DIST_DIR='.next-build'`，避免两个进程共用 `.next`。本次没有发起付费 OpenRouter 调用；由于执行环境对 LangSmith 出站网络有限制，**新 Trace 在远端页面呈现正文**仍需重启后端后创建一条新 Run 人工确认。

## v0.4.3 — Agent 执行拓扑精简（2026-09-26）

本节仅追加本次前端可视化改动，不改变历史版本内容和后端工作流。

- 概览把 11 个内部节点折叠为“规划 → 审批 → 研究 → 审查 → 成稿 → 完成”6 个阶段；阶段之间只亮起真实发生的正向路由，不再绘制跨越整张图的交叉回边。
- 补充研究、重新规划、报告修订、决策改道和跨阶段跳转单列为“反馈与跳转”，显示实际起止阶段、次数及最新事件序号。点击阶段可查看包含的内部节点及事件，点击路径可查看决策原因。
- 完整 `route_selected` 节点级路径保留在可展开记录中；概览折叠不丢失原始事件和执行顺序。窄屏改为纵向主线，避免把 11 个节点缩到无法阅读。
- `npm run test:flow` 新增阶段折叠、重复主线次数、反馈回路、决策改道、跨阶段跳转和未执行连线的确定性检查；`npm run typecheck` 通过。

验证补充：隔离目录 `.next-build` 下的 `npm run build` 通过，7 个页面生成成功。

## v0.4.4 — 阶段循环图与路由箭头修复（2026-09-26）

本节仅追加本次可视化改动；版本仍属于 v0.4 系列，Agent 工作流与事件协议未改变。

- 修复“审查 → 成稿”“成稿 → 完成”等真实已执行路由在决策被策略改道时只显示为点的问题：相邻阶段的实际路由始终画出箭头；改道仍以灰色虚线和下方反馈记录说明，不误标成真的 Replan。
- 6 个阶段改为两行折返的循环图。实际发生的补充研究、重新规划和报告修订在节点外侧形成独立回线，箭头明确指向重新进入的阶段，避免穿越节点。点击路径可查看原始路由、次数和原因。
- 下方“反馈与跳转”和可展开的完整节点路径继续保留；窄屏可横向滑动，并显示操作提示。新增确定性测试覆盖改道路由的主线箭头及常见回环的绘制路径。

验证：`npm run test:flow`、`npm run typecheck`、隔离目录下的 `npm run build` 均通过；本地桌面与窄屏预览已检查，临时预览页面和截图已清理。

## v0.4.5 — 事件颜色、确认操作与视觉系统（2026-09-26）

本节仅追加本次前端改动；版本仍属于 v0.4 系列，Agent 工作流和事件协议未改变。

- 事件时间线按后端 17 种 `RunEventType` 分别使用稳定且不重复的颜色，色点和事件标签同步着色；保留中文类型文字，避免仅凭颜色辨认。未知的新事件类型也会得到稳定的兜底配色。`npm run test:ui` 会对照后端枚举检查覆盖率与颜色唯一性。
- 审批原计划、保存修改后继续、取消运行和重新运行增加二次确认；确认文案说明是否会继续执行或产生模型调用费用。删除运行记录继续要求输入完整 Run ID，不降低原有保护。筛选、导航、复制等低风险操作保持一步完成。
- 工作台、运行记录、运行详情、统计分析和系统设置统一为偏矿物绿的低饱和视觉系统：重排标题和指标层级、减少装饰图标及重复卡片、统一表格/按钮/图表/空状态样式，并适配窄屏与减少动画偏好。现有 Agent 循环拓扑及其真实路由语义保持不变。
- 开发与预览继续使用 E 盘临时目录；本次没有读取或改写 `.env`，也没有发起模型调用。

验证：`npm run test:ui`、`npm run test:flow`、`npm run typecheck`、隔离目录下的 `npm run build` 均通过。桌面预览已检查工作台、运行记录、统计分析、系统设置和运行详情的页面骨架与无数据状态；临时预览端口未获后端 CORS 授权，因此本轮未把预览中的数据加载失败当作真实数据态验收。

## v5.0.0 — 原生工具调用与地图导航（2026-09-26）

本节追加本次工具开发的增量。版本号按用户指定升级为 5.0，代码采用 `5.0.0`；旧章节保留。

### Agent 如何使用工具

Planner、Researcher、Critic、Synthesizer、Quality Gate 共用 `ToolRuntime`。每个 Agent 在输出业务结果前，先进行有限次数的原生 Function Calling：模型返回带 `tool_call_id` 的函数调用，执行器验证权限和参数，运行真实工具，再以 `role=tool` 把结果交回模型。结束后，经过筛选的事实交给原有 Gateway，生成计划、研究结果或审核决策。计划和决策继续使用 JSON Schema；报告正文仍为 Markdown。

```text
Agent 当前职责 + 已有工具结果
  → 原生 Function Calling
  → 角色白名单 / 参数校验 / 共享预算
  → ToolRegistry：超时、重试、真实执行、LangSmith Tool Span
  → tool_call_id 对应的结果消息（循环至完成或预算边界）
  → 安全的事实上下文
  → 原有 Gateway 的结构化结果 / Markdown 报告
```

| Agent | 首版可调用工具 | 用途 |
|---|---|---|
| Planner | `web_search`、`map_route` | 明确任务范围、路线条件 |
| Researcher | `web_search`、`calculator`、`map_route` | 获取事实、计算、查询路线 |
| Critic | 同 Researcher | 针对研究结果核查缺口与冲突 |
| Synthesizer | 同 Researcher | 成稿或修订时补充核验 |
| Quality Gate | 同 Researcher | 针对报告复核事实与来源 |

Supervisor、Approval、Scheduler、Research Gate、Finalizer 继续承担确定性流程职责。工具调用发生在 Agent 节点内部，不额外增加主拓扑节点。FastAPI 与 Studio 使用同一工具执行逻辑；Studio 的线程仍由 Agent Server 管理，不自动同步到网页 SQLite。

### 工具、预算和失败处理

- 网页检索复用 OpenRouter `openrouter:web_search` server tool，保留 URL、来源摘录、检索时间和搜索摘要。没有有效 HTTP(S) 来源引用即判定失败；搜索摘要与原文摘录分别标识，不把摘要伪装成网页原文。
- 计算器使用受限 AST，仅支持基本算术，限制表达式长度、深度、指数及数值范围，拒绝代码执行、复数和非有限结果。
- 地图使用高德 Web 服务：限定城市的地点检索 → 驾车/步行路线查询 → 高德导航入口。首版仅支持中国境内同城两点；地点有歧义时返回失败，要求明确起终点。
- `RunPolicy.max_tool_calls_per_turn` 默认 3（可设 1–10），`max_tool_calls_per_run` 默认 12（可设 1–50，不能小于单次预算）。前端“高级 RunPolicy”可修改；创建后固定，重新运行默认继承。所有并行 Researcher 共享同一 Run 计数，以原子预留防止超额。
- 预算统计一次逻辑工具请求，参数错误或被拒绝的请求也计数。一次 `web_search` 内部另有搜索模型调用，一次 `map_route` 包含地点和路线等多个 HTTP 请求；因此工具次数不等于 Provider 请求次数或费用。实际 usage/cost 仍以 Provider 返回的 Trace 数据为准。
- Researcher 对“当前/最新”等动态问题要求有效网页证据；纯算术问题要求计算器；路线问题由相关任务要求地图查询。必需结果缺失会使该研究任务失败，进入现有重试、Quorum 和审核逻辑；可选工具执行失败记录局限，允许继续。工具决策阶段的 Provider 或协议错误明确失败，不启用 Mock 运行时。
- 查询意图的硬约束目前由关键词和任务范围识别；它是首版规则，不等价于完整语义分类。来源链接保留也不代表所有结论已被自动验证，仍需要 Critic / Quality Gate 审查。

### 地图数据的保存边界

按本轮确认的策略，地图的距离、耗时、POI 响应和路径数据只在当次工具阶段内使用。持久化的工具记录与事件保留调用状态、Agent、耗时等审计字段及导航链接；不保存路线数字或原始地图响应。报告只附导航入口，并提示打开高德查看最新路线、距离和耗时。导航链接本身包含必要的起终点坐标及名称。

即使 `LANGSMITH_TRACE_CONTENT=true`，地图 Tool Span 的输出也会经过专门过滤；地图成功后关闭本次工具阶段的追加调用，避免路线数字被复制到另一个工具的持久化参数。传给后续研究、成稿和审核节点的材料只包含导航入口。业务节点、模型和其他工具仍遵循既有的 Trace 内容开关。此实现描述的是 AtlasFlow 自身的记录边界；服务提供商的数据处理仍以各自协议为准。

### 页面和记录

- 新增 `tool_requested`、`tool_succeeded`、`tool_failed`、`tool_budget_exhausted` 事件，沿用 SSE 实时展示，21 种事件保持独立配色。
- `RunRecord` 增加 `tool_calls` 和 `evidence`，运行指标增加 `tool_calls`；SQLite 保存暂停或终态的完整快照。工具执行期间先通过事件时间线观察，结束后可在“工具调用与来源”查看摘要、来源和导航入口。
- 报告生成后补齐工具实际返回的来源/导航链接，再交 Quality Gate 验收；原有任务引用仍保留。没有调用地图时不会凭空生成导航链接。

### 关键文件

| 文件 | 本版职责 |
|---|---|
| `backend/src/atlasflow/agents/tool_runtime.py` | 角色工具白名单、原生调用循环、共享预算、结果过滤、来源链接 |
| `backend/src/atlasflow/agents/workflow.py` | 五类 Agent 接入工具阶段，合并并行调用记录与证据 |
| `backend/src/atlasflow/agents/gateway.py` | 使用已有工具证据生成业务结果，修正旧版“没有工具”的提示 |
| `backend/src/atlasflow/tools/amap.py` | 高德地点消歧、驾车/步行查询与导航 URL |
| `backend/src/atlasflow/tools/builtin.py` | 网页证据解析与受限计算器 |
| `backend/src/atlasflow/providers/openrouter.py` | `tool_choice`、工具消息、并行调用开关、server tool 预算 |
| `backend/src/atlasflow/observability.py` | 工具阶段 Trace 与地图响应过滤 |
| `frontend/components/create-run-form.tsx` | 工具预算配置 |
| `frontend/components/run-detail.tsx` | 调用记录、来源和地图入口 |
| `backend/tests/test_tool_runtime.py` | 原生调用、预算竞争、失败语义、地图留存和工作流集成回归 |

### 配置与尝试

项目根目录 `.env` 和 `.env.example` 已增加 `AMAP_API_KEY=` 占位。填写高德 **Web 服务 API 类型 Key** 后重启 FastAPI；使用 Studio 时也重启 Agent Server。地图 Key 未填时，普通研究、搜索和计算仍可运行，地图调用会明确提示缺少配置。网页检索继续使用已有的 `SEARCH_*` 配置；`SEARCH_API_KEY` 和 `SEARCH_MODEL` 为空时复用 LLM 配置。主模型需要同时支持原生 Function Calling 和 JSON Schema。

无需新建环境或安装新依赖，继续使用 E 盘的 `langchain` 环境和原有启动方式。建议从这些任务验证：

1. `1+1=?`：时间线出现计算器调用，详情保留结果 `2`。
2. `检索今天人民币兑美元的汇率，注明报价方向、数据日期和来源`：先获取网页证据，再生成带来源的报告；查询失败时明确暴露失败原因。
3. `介绍北京故宫附近的游览安排，并提供从北京站到故宫博物院的驾车导航`：配置地图 Key 后，报告和工具记录出现高德导航入口，路线数字不进入历史记录。

接口依据：[OpenRouter Function Calling](https://openrouter.ai/docs/guides/features/tool-calling)、[OpenRouter Web Search Server Tool](https://openrouter.ai/docs/guides/features/server-tools/web-search)、[高德路线 API](https://lbs.amap.com/api/webservice/guide/api/newroute)、[高德导航 URI](https://lbs.amap.com/api/uri-api/guide/travel/route)。

RAG、向量数据库及网页全文抓取留待后续版本。本轮自动化验证使用注入的测试 Gateway 和 HTTP transport，不消耗真实模型、搜索或地图额度；实际账号权限、远端模型支持情况和真实检索质量需要配置后在线验证。

### v5.0 验证结果

E 盘 `langchain` 环境的后端测试为 **75 passed**，覆盖调用消息往返、并行预算竞争、越权/非法参数、必需工具失败、Provider 中断后保留调用记录、地图 Trace 过滤及 SQLite/报告集成。Ruff、`npm run typecheck`、`npm run test:ui`、`npm run test:flow` 均通过；`npm run build` 在 E 盘 `.next-build` 隔离目录完成，7/7 页面生成成功。后端仅有 Starlette 上游 anyio alias 弃用警告。

### v5.0 修补 — 工具阶段 OpenRouter 404（2026-09-26）

真实运行反馈 `No endpoints found that can handle the requested parameters`。检查发现工具阶段同时发送 `parallel_tool_calls=false` 和 `provider.require_parameters=true`；本次配置的 `deepseek/deepseek-v4.1-flash` 的公开端点能力包含 `tools`、`tool_choice`，但未声明 `parallel_tool_calls`。即使参数值为 `false`，严格路由仍要求端点支持该参数，因此候选端点被排除。依据：[模型端点能力](https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints)、[OpenRouter 参数支持筛选](https://openrouter.ai/docs/guides/routing/provider-selection)。

- 工具决策请求省略可选的 `parallel_tool_calls`，保留 `tools`、`tool_choice` 和严格参数检查。用户无需更换当前模型、API Key 或修改 `.env`。
- 模型一次返回多个调用时，本地执行器逐条执行，逐条验证权限、参数和预算；返回消息按各自的 `tool_call_id` 配对。重复/缺失 ID 在执行前拒绝。
- 若预算在批次中耗尽，仅保留已经执行的结果并结束该工具阶段，不把缺少部分工具响应的消息序列再次发给 Provider；必需工具的成功条件仍然检查。地图结果的临时使用和留存边界保持原策略。
- 修复后重启 FastAPI；如果从 Studio 调试，也重启 Agent Server，再创建新 Run。已失败的历史 Run 不会自动重新执行。

验证：增加可选参数导致 404 的回归模拟，以及批量调用顺序、单次/整轮预算、重复 ID 检查；后端 **79 passed**，Ruff 通过。本次在线检查仅读取公开模型能力列表，没有发起收费模型调用，因此真实账号下的重跑结果仍需验证。版本保持 `5.0.0`。

### v5.0 调整 — 每个工具一个文件（2026-09-27）

为便于阅读与扩展，工具实现按工具名称分别存放；每个文件包含该工具的参数模型、实现类和专属辅助方法：

```text
backend/src/atlasflow/tools/
├── calculator.py        # CalculatorArguments / CalculatorTool：受限算术计算
├── web_search.py        # WebSearchArguments / OpenRouterWebSearchTool：网页搜索
├── knowledge_search.py  # KnowledgeSearchArguments / KnowledgeSearchTool：知识检索
├── map_route.py         # AmapRouteArguments / AmapRouteTool：高德路线与导航
├── base.py              # 公共工具接口、上下文、返回值和风险等级
├── registry.py          # 公共注册表与执行器
└── __init__.py          # 公共接口导出
```

原 `builtin.py` 的三个工具已完整迁移至各自文件，该聚合文件移除；原 `amap.py` 更名为 `map_route.py`。旧章节中的文件名保留为历史记录，以本节目录为准。FastAPI 装配、Studio、测试和在线冒烟脚本已同步更新 import；工具名称、参数、调用逻辑和权限保持不变，知识检索仍未接回 Agent 主流程。

扩展新工具时，在 `tools/` 下创建对应文件，继承 `BaseTool` 并实现 `run()`，再在装配入口注册，并按需更新 `ROLE_TOOLS`。本次版本仍为 `5.0.0`。

验证：E 盘 `langchain` 环境下后端 **79 passed**，Ruff 通过；未执行真实 Provider 调用。

### v5.0 调整 — 结构化能力规划与可扩展工具注册（2026-09-27）

本节为追加变更记录，版本仍为 `5.0.0`。上节中的 `ROLE_TOOLS` 扩展方式已由本节替代，旧文保留作为历史记录。

#### 本轮做了什么

- 原 `ResearchTask` 新增 `requires_fresh_data` 和 `required_capabilities`。Planner 根据每个任务的目标、成功标准决定能力要求；Researcher 不再通过“当前、汇率、导航”等关键词或算式正则分类问题。
- Planner、Critic 新增任务共用动态能力 Schema 和语义校验。新模型输出必须显式声明两个字段；未知能力、重复能力、缺字段、不一致的新数据要求会进入结构化修复。旧快照仍可用默认值读取，不会自动迁移其语义。
- 能力与具体工具分离：同一能力可注册多个实现，由 LLM 从可用候选中选择具体工具并生成参数；必需能力满足后仍可动态补充其他工具。没有必需能力时仍允许自主按需调用。
- 工具文件声明能力、允许角色、风险、配置状态及专属留存规则。缺少高德 Key 时，能力仍可如实进入计划，但执行会明确报告不可用，不得靠删掉要求或模型记忆冒充完成。
- `tools/catalog.py` 成为 FastAPI / Studio 共用的工具注册入口。新增普通工具主要是“一个工具文件 + 一处注册 + 测试”，不必改工作流语义分支、固定能力枚举或前端能力清单。
- `ToolCallRecord` 新增计划版本、能力记录、复用来源。必需能力的历史成功记录只在同任务、同计划版本、同 Agent 且非新数据任务下复用，其他任务和旧计划不能替代当前任务执行。
- 计算器和网页搜索支持同一工具阶段内的相同参数去重；失败结果、地图结果不缓存，不做跨阶段／跨 Run 缓存。重复请求仍消耗请求预算槽，复用记录以 `reused_from_call_id` 标明来源。
- 原有地图数值不持久化、预算竞争保护、Function Calling 批量消息配对、OpenRouter 可选参数兼容修补均保留；RAG 和数据库结构没有另行重构。

#### 当前运行流程

```text
Query
  → Planner（可按需调用工具；根据动态能力目录输出结构化任务）
  → Approval（检查或修改目标、依赖、成功标准、能力、新数据要求）
  → Researcher（按任务契约进入工具阶段，LLM 选择工具和参数）
  → ToolRuntime / ToolRegistry（权限、参数、预算、去重、执行、证据）
  → ResearchResult
  → Critic（审查证据与成功标准；Accept / Supplement / Replan）
  → Synthesizer → Quality Gate → Finalizer
```

Critic、Synthesizer 和 Quality Gate 仍可以调用工具。`requires_fresh_data` 约束重新获取数据，不保证返回页面本身足够新；来源日期、报价口径、任务覆盖等仍由 Researcher 和 Critic 审查。“某项能力成功执行”不等于“任务已被充分回答”。

#### 前端和接口

审批面板可展开能力目录，查看可用状态、配置缺口及新数据支持；每个任务支持编辑能力 ID、新数据要求，任务详情与 SSE 展示保留这些字段。未知能力的人工编辑返回 HTTP 422，Run 保持等待审批。

- `GET /api/v1/tools`：工具实现、参数 Schema、能力与角色等元数据。
- `GET /api/v1/capabilities`：Researcher 能力目录、可用实现、本地配置不可用原因。这里不是远端服务健康检查。

完整的文件职责、扩展模板、留存钩子、复用边界与测试步骤见 [工具扩展指南](docs/tool-extension.md)。实际天气工具尚未接入，测试中的天气工具只用于证明新能力能在不修改工作流分支的情况下被注册、规划和执行。

#### 验证与使用

使用 `E:\conda_envs\langchain` 环境：后端 **97 passed**，Ruff 通过；前端 `npm run typecheck`、`npm run test:ui`、`npm run test:flow` 通过。新增覆盖能力动态枚举、多实现选择、Planner / Critic 修复、配置缺失、角色限制、跨任务／跨版本隔离、去重、2 → 3 补充任务执行和非法审批编辑。仅保留 Starlette 上游 anyio alias 弃用警告。

没有修改 `.env`，没有发起收费 Provider 调用。真实模型的规划质量、账号权限、搜索结果时效仍需在线验证。重启 FastAPI 和 Studio（若使用），刷新网页后创建新 Run；历史 Run 不会自动重跑或重写。

### v5.0 修补 — Planner 边界与北京一日游失败链（2026-09-27）

根据 Trace `8bb45174-b499-4157-8101-de4a3e7bab67` 修复，不修改历史章节，版本仍为 `5.0.0`。

#### 职责和数据流

- Planner 默认不进入模型工具决策，直接生成结构化研究任务。前端高级 RunPolicy 新增 `planner_allow_research`，默认关闭；开启后每次规划最多一次轻量查询，只开放声明 `planning_safe=True` 的工具。地图在注册表执行层也禁止 Planner 调用，不能靠模型忽略提示词绕过。
- Planner 提示词不再将“整合／撰写最终报告”作为 ResearchTask；这些工作交给 Synthesizer。Synthesizer 工具阶段只复用已有材料，不再发起新查询。
- 原始 `query` 不再拼接工具结果。所有 Gateway 方法通过独立 `tool_context` 接收证据；Researcher 的工具阶段显式接收完整任务契约、上游 `dependency_results`，然后才选择调用参数。
- 工具决策与结构化生成均提供当前时间，要求当前信息查询不能自行套用旧年份。仍须审查来源实际日期，不能把检索时间当作数据时间。

#### 运行失败与可观测性

- OpenRouter 每次 `chat()` 增加独立 `provider.openrouter.chat` Trace，保留响应 `finish_reason`、内容长度及 Provider 元数据。结构化修复产生的第二次请求不再隐藏在同一个模型方法中；网页运行的模型调用指标按实际记录去重统计。
- Researcher 明确要求 `summary/findings/limitations/confidence`，不输出任务定义字段或推理过程。JSON 截断时拒绝结果，在原有一次修复预算内扩大输出上限（最多 16000）；修复提示列出目标顶层字段，仍需通过完整契约校验。
- 工具决策输出上限从 1000 调整为 3000，截断时最多扩至 6000；不执行截断响应中的调用。缺少必需调用时给模型有限的纠正机会，不能无上限重试。失败仍保留为失败，没有降低 quorum 或伪造研究成功。
- 应用返回 `status=failed` 时，根 Trace 和追踪片段同步标记业务失败，而不再只因为异常已被捕获就显示完成。函数级工具节点仍可能正常返回 `success=false`，应结合输出判断。
- 前端事件时间线支持展开错误原因。

#### 地图与多段行程

- 地点解析加入明确等价名称规范化，例如“圆明园／圆明园遗址公园”和“北大／北京大学”，仍要求唯一候选，不采用模糊相似度或盲取首项。多候选、无地点、无路线、网络、超时、Key 平台和配额问题分别返回安全错误码；不回传可能包含 Key 的原始异常／服务端文字。错误码依据：[高德官方说明](https://lbs.amap.com/api/webservice/guide/tools/info)。
- 新增独立文件 `tools/map_itinerary.py`，提供 `map_itinerary` 能力：一次有序查询 2–6 个站点，最多 5 段；往返时末尾重复起点。所有路段成功才算能力完成，中途失败明确标明第几段，已成功部分只保留导航链接。
- 一次多站点调用消耗一个工具请求槽，内部最多 5 段路线，每段最多 2 个地点请求和 1 个路线请求；短时限流可有两次退避重试，整工具仍受既有超时约束。两个地图工具共用请求节流器，默认约每 1.05 秒启动一次请求；高德 `10004/10021` 或 HTTP 429 有界重试，不重试错误 Key／日配额等确定性配置问题。
- 地图数值依然不持久化。多站点工具直接只返回链接；单路线成功后工具阶段直接收尾，不再发送携带临时地图数值的后续模型请求，避免新增 Provider Trace 泄漏这些数据。前端及报告可展示全部路段导航链接。

#### 本轮验证

新增 `backend/tests/test_failure_recovery.py` 覆盖 Planner 默认零调用与单次调研、禁止地图、原始 query 不变、依赖结果传递、JSON／原生工具截断、四段行程、部分失败、名称别名、错误码脱敏、限流重试和业务失败追踪。

小范围真实验证：当前配置的模型用一次请求生成合法研究计划，选择了 `map_itinerary`；一次请求返回合法 ResearchResult。高德“北京大学 → 颐和园 → 圆明园 → 天安门广场 → 北京大学”在增加节流后四段全部成功，返回 4 个导航链接，没有保存距离／耗时。真实验证消耗了两次模型请求和少量地图额度；未启动完整真实研究 Run，未修改 `.env`、旧 Run 或旧 Trace。

最终回归：E 盘 `langchain` 环境下后端 **111 passed**，Ruff、`git diff --check`、前端 `typecheck`、`test:ui`、`test:flow` 均通过。保留一个 Starlette 上游弃用警告；本轮未重新执行前端生产构建。

重启 FastAPI 和 Studio（若使用），刷新前端后创建新任务。默认保持“允许 Planner 轻量调研”关闭。地图还有真实歧义、配额或网络问题时会明确失败；模型和外部服务的不确定性不能由本次修补保证永久消除。

### v5.0 修补 — 报告阅读器与下载（2026-09-28）

本次只调整报告的前端展示与导出，不改变 Agent 流程、模型提示词和数据库，也不重新生成已有报告；版本保持 `5.0.0`。

#### 展示与使用

运行详情 → **研究报告**：报告改为独立文档布局，蓝灰配色、宋体风格主标题、分层正文、引用块、斑马纹表格和代码块。桌面端显示章节目录，点击可跳转，滚动时高亮可见章节；窄屏目录可折叠，宽表格在自身区域横向滚动。目录来自实际解析后的 Markdown 标题，重复标题拥有不同锚点，代码块不会误入目录。

- **下载 Markdown**：保存原始报告文本，便于后续编辑；不额外插入运行状态说明。
- **下载 HTML**：保存自带样式的单文件文档，可离线阅读，保留研究问题、运行状态、时间、运行编号和来源／导航链接，无需启动 AtlasFlow。未完成验收的报告保留提示。
- **打印 / 存为 PDF**：打开浏览器打印窗口，选择“另存为 PDF”。打印仅包含报告文档，隐藏侧栏、工具栏、运行指标及其他面板；设置 A4 页边距、表头重复和代码换行。此入口是浏览器打印，不是服务器直接生成 PDF。

下载、打印不会调用模型或工具。刷新前端即可查看历史报告的新排版，无需重跑研究任务。网页无法替用户指定本机下载目录：请先把浏览器下载目录设为 **E 盘**，或启用“下载前询问保存位置”，PDF 保存对话框也选择 E 盘。

#### 实现与安全

- `frontend/components/report-view.tsx`：报告阅读器、目录、下载动作和未验收提示。
- `frontend/lib/report-document.ts`：页面与 HTML 共用的文档样式、Markdown 标题锚点、文件名清理、HTML 文档封装及 Blob 下载与释放。
- `frontend/app/report.css`：阅读器布局、移动端与报告专用打印样式。
- Markdown 继续禁用原始 HTML；链接仅开放 HTTP(S)、邮件与页内锚点，图片以链接展示而不自动请求远端资源。HTML 导出来自安全渲染后的 DOM，标题转义，内置 CSP 禁止脚本。中文下载采用 UTF-8；导出文件不包含 API Key、工具参数、Trace 或运行日志。

#### 验证

前端 `typecheck`、`test:ui`、`test:flow`、新增 `test:report` 与生产构建通过。新增测试覆盖表格与引用渲染、重复标题、代码块、危险链接、空报告、未验收提示、HTML 封装、原文保留、中文文件名和下载 URL 释放。

`test:report:browser` 可对已启动的前端进行可选浏览器验证（例如 `npm --prefix frontend run test:report:browser -- http://localhost:3210`）。使用本机 Chrome／Edge、E 盘隔离配置和测试数据拦截，不连接真实研究任务：已验证桌面／手机页面、目录、实际 Markdown／HTML 下载、打印隔离及 PDF 输出。截图、下载文件及浏览器测试配置均在 `E:\codex\agent\build\report-browser`，未发起真实模型调用。

### v5.0 修补 — 固定报告目录与成稿验收（2026-09-28）

根据本轮 grill-me 决策继续修补，版本仍为 `5.0.0`。历史报告文本不自动改写；后端成稿规则作用于新 Run。

#### 报告目录

报告页外层面板曾设置 `overflow: hidden`，使目录的 `position: sticky` 被截断；宽度小于等于 1180px 时目录还被改为 `position: static`。现在仅对报告页解除外层裁剪：桌面目录在滚动正文时保持在视口左侧，到报告结束自然停止；中窄屏改为顶部粘附的折叠目录，手机端避开固定顶栏。切换桌面／手机宽度时同步默认展开状态，仍可手动展开。打印继续隐藏目录。

#### Synthesizer、修订与 Quality Gate

- Synthesizer 正文从 `## 摘要` 开始，不重复页面已有的研究问题大标题；保留 `## 摘要`、`## 局限`、`## 来源`，其他章节根据题目自由组织。先答核心问题，再分析，篇幅按问题复杂度调整。需要比对精确项目时才使用表格。
- 已完成研究结论就近标注真实 `[task:任务ID]`；时效数据、价格、日期等事实在相关句／段附近引用实际工具来源，文末列出来源和导航。无外部证据需求的算术题不强加网页引用。没有核实到关键数据时标明“未核实”和证据缺口，不生成貌似确定的实时数值。
- 修订仍返回完整 Markdown，但提示词要求集中修改 Quality Gate 指出的段落，保留已经核实的结论、任务引用、来源链接和章节顺序。`append_references` 会将遗漏的工具链接加入已有的 `## 来源`，避免另造重复来源章节。
- Quality Gate 的模型审查增加结构、就近引用、未核实关键事实和题型适配判断；随后执行确定性验收：必要章节、任务 ID 是否对应当前研究结果、Markdown 链接是否来自已获取的工具证据／导航、要求当前数据但没有来源。可修问题改走 `revise`，缺失关键时效证据改走 `replan`；预算耗尽时保留为 `completed_with_warnings`，报告页明确标注“未通过最终验收”。确定性检查只能确认来源映射和结构，事实是否被来源真正支持仍由 Quality Gate 结合研究结果评估。

#### 验证与运行

使用 E 盘 `langchain` 环境：后端 **117 passed**、Ruff 通过；前端 `typecheck`、`test:report`、`test:ui`、`test:flow`、生产构建通过。新增的后端测试覆盖算术无需外链、代码块中的假标题、虚构任务／URL、缺失当前数据、来源区合并、验收改走修订与预算耗尽状态。`test:report:browser` 使用长测试报告实测桌面／手机滚动时目录仍粘附，并再次验证下载与打印；测试产物均在 E 盘。此轮未发起真实模型 Run，模型对新提示词的实际成稿质量仍需新 Run 验证。

重启后端和 Studio（若运行），重新启动或刷新前端；旧报告会获得新的目录行为，新成稿与 Quality Gate 规则只影响新运行。

### v5.0 增量 — 网页深读、天气预报与景点核对工具（2026-09-28）

本节只追加本次工具开发内容；项目大版本仍为 `5.0.0`。新增三个独立工具文件：`web_fetch.py` 对公开网页做有引用的深读；`weather_forecast.py` 获取带日期、查询时间和署名的短期天气预测；`poi_details.py` 用高德 POI 做当次地点核对。现有 `tools/catalog.py` 注册后，FastAPI 和 Studio 共用这些能力，Planner 默认仍不调用这些工具。

建议旅游研究按“搜索场馆官网 → 深读开放／预约页 → 查询天气 → 临时核对 POI → 路线／行程导航”拆分任务。网页深读若没有目标 URL 的引用则返回失败；天气是预测而非实测。高德 POI 营业等字段不持久化、不写入报告，也不上传 LangSmith 的工具输出；高德平均消费不是票价。报告中的营业、门票和预约信息应另引场馆官网。这样将报告可引用的事实与仅当次可用的地图数据分开。

天气默认使用 Open-Meteo 非商业免费 API，不需要新 Key；如需商业部署，可在 `.env` 自行填写可选 `OPEN_METEO_API_KEY` 对应的订阅密钥。`AMAP_API_KEY` 继续供地点与路线工具使用；未改动已有 `.env`。Open-Meteo 数据需在报告中署名；高德 POI／天气数据不可直接缓存。详细的能力、留存规则、失败语义和扩展方法见 [工具扩展指南](docs/tool-extension.md)。

使用 E 盘 `langchain` 环境完成离线测试：后端 **124 passed**。新增测试覆盖网页引用校验和内网 URL 拒绝、天气地点歧义及密钥脱敏、高德 POI 临时输出、运行记录与 LangSmith 脱敏。本次未调用真实供应商 API；请重启 FastAPI／Studio、新建 Run 后，用配置好的模型和 Key 做一次在线验收。OpenRouter `web_fetch` 属于 beta 服务，实际可用性取决于账户和模型路由。

### v5.0 修补 — 天气地点解析与工具预算（2026-09-28）

本次只追加故障分析与修补记录，大版本仍为 `5.0.0`。对应 Run `6d7a6bf2-d39b-44bb-be15-5917678548cb` 的本地事件显示：问题“给出未来三天的北京的天气和出行计划”先后把“北京”“北京, 中国”“北京市”等传给天气工具，前九次天气查询都因地名歧义或未找到城市而失败，第十次才取得天气；该 Run 保存的策略是单次 5 次、全程 15 次。此前总共 15 次工具请求已耗尽配额，后续 T4 导航、Critic 补充任务和重规划研究无法再调用工具，最终因重规划后研究 Quorum 0/3 而失败。LangSmith 私有页面在当前环境中无法直接打开；分析来自同一 Trace ID 对应的本地 Run/Event 数据。

天气工具现在将“北京市”规范为“北京”再查询，针对逗号分隔的国家／省市限定词逐一筛选；北京等重名结果可在唯一首都候选存在时确定性选取，其他城市只有明显的人口优势才自动选取，否则仍返回歧义，避免随意选择第一条。中文／英文城市名分别采用相应地理编码语言。离线回归覆盖“北京”“北京市”“北京, 中国”“Beijing, China”一次解析成功，以及仍应判歧义的 Springfield。

新建 Run 的默认工具预算从 **3/12** 提高到 **每次 Agent 决策 5 次、整个 Run 20 次**；标准、快速、严格预设及自定义表单同步。Planner 调研的单次上限仍为 1 次，失败调用仍计入预算，并行 Researcher 仍共享全程预算。**已有 Run 的策略保存在记录中，不会被追溯修改**；需重启服务并新建 Run 验证。单纯提高预算不能修复地名解析错误，因此两项修补同时进行。

验证：E 盘 `langchain` 环境后端 **129 passed**、Ruff 通过；前端 `typecheck`、`test:ui`、`test:flow` 通过。由于当前环境无法直连 Open-Meteo，尚未完成真实天气接口联调；新 Run 的在线效果仍需实际运行核对。

### v5.0 修补 — 天气地点解析与工具预算（2026-09-28）

本次只追加故障分析与修补记录，大版本仍为 `5.0.0`。对应 Run `6d7a6bf2-d39b-44bb-be15-5917678548cb` 的本地事件显示：问题“给出未来三天的北京的天气和出行计划”先后把“北京”“北京, 中国”“北京市”等传给天气工具，前九次天气查询都因地名歧义或未找到城市而失败，第十次才取得天气；该 Run 保存的策略是单次 5 次、全程 15 次。此前总共 15 次工具请求已耗尽配额，后续 T4 导航、Critic 补充任务和重规划研究无法再调用工具，最终因重规划后研究 Quorum 0/3 而失败。LangSmith 私有页面在当前环境中无法直接打开；分析来自同一 Trace ID 对应的本地 Run/Event 数据。

天气工具现在将“北京市”规范为“北京”再查询，针对逗号分隔的国家／省市限定词逐一筛选；北京等重名结果可在唯一首都候选存在时确定性选取，其他城市只有明显的人口优势才自动选取，否则仍返回歧义，避免随意选择第一条。中文／英文城市名分别采用相应地理编码语言。离线回归覆盖“北京”“北京市”“北京, 中国”“Beijing, China”一次解析成功，以及仍应判歧义的 Springfield。

新建 Run 的默认工具预算从 **3/12** 提高到 **每次 Agent 决策 5 次、整个 Run 20 次**；标准、快速、严格预设及自定义表单同步。Planner 调研的单次上限仍为 1 次，失败调用仍计入预算，并行 Researcher 仍共享全程预算。**已有 Run 的策略保存在记录中，不会被追溯修改**；需重启服务并新建 Run 验证。单纯提高预算不能修复地名解析错误，因此两项修补同时进行。

### v5.0 修补 — web_fetch 直接抓取与重复失败抑制（2026-09-28）

此前的实现依赖 OpenRouter 最终聊天消息中的 URL 引用字段，实际接口不保证返回该字段，导致有文本响应仍报 WEB_FETCH_NO_CITATION。本次以本节说明为准，旧章节保留历史记录。

- web_fetch.py 改为 WebFetchTool，直接读取 HTTP HTML／纯文本；从实际响应提取可见正文、页面标题和来源，不再内部调用 LLM，也不依赖 annotations。工具名称和能力 ID 仍为 web_fetch，FastAPI／Studio 共用注册入口，网页抓取不需要单独 API Key。
- Evidence 保存原始 URL、最终 URL、实际重定向链、HTTP 状态、抓取时间和摘录在提取文本中的位置；按 focus 选择最多 3 段限长原文。正文不代表事实已经通过审查，时效、相关性和是否足以支撑结论仍由 Researcher／Critic 判断。
- 请求逐次检查 URL、DNS 结果与重定向，连接到已检查的公网 IP，同时保留原域名的 Host、SNI 和 TLS 证书校验；禁用环境代理及跨请求 Cookie，不读取本机或内网地址。最多 3 次重定向、30 秒总时限、1 MB 正文上限。
- 对 403／404、空正文、不支持的内容类型等返回明确错误。新增工具扩展钩子 failure_scope() 和 ToolCallRecord.retryable：web_fetch 按 URL 标识失败目标，仅修改 focus 不会绕过重复失败检查。同阶段及后续已收到该失败记录的任务若再次请求该 URL，将记录 suppressed=true 事件并结束该工具阶段，不再次发 HTTP 请求或占用工具预算。换 URL 仍可调用；超时、429／5xx 等临时失败仍允许在已有预算内重试。历史记录未提供 retryable 的不追溯锁定。
- 当前支持 HTML／XHTML／纯文本／Markdown，处理 UTF-8 和常见中文编码；不执行 JavaScript，不绕过登录或验证码，不解析 PDF。请求未压缩正文，若网站强制返回不支持的压缩响应，会明确失败并提示换来源。

验证：E 盘 langchain 环境后端 **155 passed**、Ruff 通过，前端类型检查通过。在线只读验证已成功读取国博参观页 https://www.chnmuseum.cn/cg/ 与中国科学技术馆首页 https://www.cstm.org.cn/，均生成了真实网页摘录与来源证据；这两次验证没有调用模型或使用 API Key。尚未重跑整个多 Agent 研究任务。

重启后端和 Studio 后创建新 Run 即可使用。新建 Run 默认预算仍为单次 5／全程 20；本次更新不改写旧报告或历史运行结果。

### v5.0 修补 — 结构化导航地点与歧义恢复（2026-09-28）

针对 T3 反复出现 `AMAP_POI_AMBIGUOUS`，本次把地点约束从“单个名称字符串”扩展为模型可生成的结构化参数。仍属 `5.0.0`，历史章节、运行与报告不改写。

- `map_route` 的 `origin`／`destination` 和 `map_itinerary` 的每个 `stops` 元素支持 `{"name":"中国国家博物馆","district":"东城区","address":null,"entrance":null}`。城市在外层 `city` 指定；兼容旧名称字符串和可精确匹配的完整地址字符串。
- Agent 工具阶段优先生成结构化地点，只填写用户要求、上游行程或已有依据能支持的限定信息；未知字段留空，不编造入口、地址、POI ID 或坐标。Planner 仍不承担路线查询。
- 地图解析从前 3 个候选扩展到最多 25 个，核对名称、已知区县、完整地址和指定入口；支持完整城市前缀、入口括号／连字符差异，以及相同 POI ID 且关键字段一致的重复结果消除。不把单个不相关结果当成功，也不把景区的停车场／售票处视为景区或指定入口。此处的唯一匹配仅针对本次返回候选，不保证全球或全量数据唯一。
- 失败明确区分起点／终点，多站行程附路段编号。Agent 能在原预算内依据提示补充或修正条件；无法确认时保留缺口，说明需要用户澄清。本轮没有新增交互式地点选择页面或暂停／恢复流程，也没有把原始高德候选送给模型挑选。
- `ToolCallRecord.failure_scope` 兼容旧记录；地图只保存规范化请求的 SHA-256 指纹，不保存原始地点参数或候选。相同参数的确定性失败会在当前阶段以及已收到失败历史的后续阶段被拦截，不再次请求或占工具预算；修正地点条件后允许重试。暂时性网络失败不锁定；未合并记录的并行分支不保证共享抑制。
- 高德原始候选仍只在工具内存中处理，不进入数据库、事件或 LangSmith；持久化仍只保留必要状态、失败指纹、安全提示与允许的导航链接。没有新增 Key 或修改 `.env`。

验证：E 盘 `langchain` 环境后端 **175 passed**、Ruff 通过、前端类型检查通过；新增 `backend/tests/test_map_disambiguation.py` 覆盖结构化参数、区县／地址消歧、入口选择、重复候选、无关单结果拒绝、失败定位、模型收到安全提示后修正参数、跨轮失败指纹与旧格式兼容。真实高德只读测试中，结构化“王府井（东城区）→ 中国国家博物馆（东城区）”成功生成导航链接；“天坛公园＋东门”的测试未找到可确认的精确匹配，按安全策略保留 `AMAP_POI_NO_MATCH`，未擅自选择其他地点。尚未重跑整个多 Agent 研究任务。

重启后端及 Studio 后新建 Run 使用。此补丁降低可避免的地点歧义，但不承诺所有地点都能自动解析；不能为追求工具成功率而牺牲导航准确性。


### v5.0 修补 — 多工具组合任务、能力任选组与预算收尾（2026-09-29）

本次针对 Trace `b722d260-7319-4fd2-a514-99c6468cb535` 对应的北京三日游运行进行修补。该运行已经使用新版地点匹配，但仍出现 POI 成功即结束工具阶段、路线二选一被声明为全部必需、非法请求占用预算，以及预算用尽后继续重规划等组合问题。版本仍为 `5.0.0`，只追加记录，不改写历史运行。

- **多工具阶段**：临时工具成功后，后续模型只收到成功状态、应用生成的安全摘要与提示，`data` 为空，不包含高德 POI、地址、坐标、距离、耗时或营业字段。当任务还有其他必需能力时继续执行；所需能力完成且本轮有临时工具成功后结束阶段。原始候选不进入后续模型或 LangSmith，允许的导航链接通过工具记录供成稿使用。
- **能力契约**：`required_capabilities` 仍表示全部必须成功；新增可选 `capability_alternatives`，组内任选一种、组间全部满足。例如搜索加路线：`required_capabilities=["web_search"]`，`capability_alternatives=[["map_route","map_itinerary"]]`。Planner、Critic 任务 Schema、校验、Workflow、Runtime、审批编辑与任务抽屉接通新字段；旧记录缺省为空数组。工具能力完成仍不代表路线所有路段或研究成功标准已经满足，须由后续审查判断。
- **工具开放与拒绝**：模型持续看到当前 Agent 已授权且可用的完整工具目录，`tool_choice` 用来引导优先补齐缺失能力，不再把其他已授权工具隐藏后误报 Agent 无权限。执行前的目录和参数校验保留；未知、不可用或越权工具不执行，参数错误与工具不可用有独立错误码。
- **预算**：参数或地点约束校验失败只记录 `tool_failed` 拒绝事件（`executed=false`、`budget_counted=false`），不产生实际调用记录、不消耗 5／20 工具预算。单工具阶段最多接收 3 次此类拒绝，模型循环也有上限，防止无限纠错。`transit` 仍不支持，会提示合法值为 `walking`／`driving`，不能默默转换出行方式。真正执行的失败仍计预算。
- **明确入口保护**：原始用户问题通过 ToolContext 传入地图安全校验。对“从某场所东／西／南／北门出发”等显式格式做窄范围确定性检查，支持北大简称、结构化入口和门号；拒绝把该地点替换成场所整体、其他门或同名地铁站。此检查不负责工具语义分类，也不是任意自然语言地点约束的完整解析器；未识别格式仍需模型和审查验证。无可靠入口匹配时明确保留缺口，不能伪造成功。
- **耗尽后的收尾**：实际工具预算为零时不再发起工具决策模型请求；必需能力未完成时返回不可通过任务重试解决的预算错误。Researcher 不再为此重复尝试。Quorum 不足时明确失败、保留已有记录，不重规划；已具备可成稿的研究结果时，Critic 不再追加任务或重规划，改为整理已有证据并列出缺口。QualityGate 同样不会因预算已空而重开研究，保留报告并标记带警告。预算不重置、不擅自提高。
- **完整链接**：工具上下文优先保留完整导航条目，不再直接在第 4500 字符处切断 URL；空间不足时省略整个条目并注明完整数据在工具记录中，避免把展示截断误判为导航工具失败。

验证：E 盘 `langchain` 环境后端 **187 passed**（包含新增 12 项组合故障回归）、Ruff 通过；前端 `typecheck`、`test:ui`、`test:flow` 全部通过。覆盖搜索→POI→路线连续执行、任选能力、非法交通模式纠错、不泄漏临时数据、入口不可降级，以及真实 LangGraph 编排下 Quorum／Critic／Quality 三条预算耗尽收尾路径。测试使用确定性供应商响应，不代表完成了真实模型端到端联调；本轮未重新请求付费模型运行整段三日游任务。

重启后端和 Studio 后，创建新 Run 验证。已有失败 Run 的记录、策略和结果不会自动重写；`.env` 无需新增配置。

### v5.0 修补 — Planner 结构化输出恢复与诊断（2026-09-29）

对应 Run `17058b58-4a6f-4798-a6f4-371b6be8e3a5`：Planner 第一次生成用满 4096 个输出 Token，`finish_reason=length`，正文 129 字符；第二次提高上限后返回 `stop`，但正文仅 1 字符。此前两个请求共用固定的两次尝试，最终通用 JSON 错误掩盖了第一次截断。本次仍属 `5.0.0`，不修改历史记录。

- 结构化输出现在分别给予一次截断恢复、一次格式／契约纠错，单次结构化生成最多三次请求；同类失败累计两次即停止。纯截断或纯格式失败仍最多两次，不无条件增加调用。输出上限仅因截断翻倍，恢复上限为 16000；Planner 初始上限仍为 4096。
- 区分 `OUTPUT_TRUNCATED`、`EMPTY_CONTENT`、`CONTENT_TOO_SHORT`、`INVALID_JSON`、`CONTRACT_INVALID`。`content=null` 按空正文处理，而不是转换成字符串 None。即便截断正文恰好可以解析也不接受；只有完整解析并通过原有任务契约校验才返回计划。
- 最终失败信息附带每次尝试的错误类别、finish reason、正文字符数和输出上限，不再只保留最后一次 JSON 错误。启用 LangSmith 追踪时，每次校验记录为 `model.structured-output.validation` 子调用，包含恢复成功的 `VALID` 记录；该诊断节点不接收或保存原始模型正文。已有模型节点的内容追踪策略不变。失败链通过既有节点失败事件在网页中展示，不额外新增事件类型。
- Planner 提示补充紧凑输出要求：理由一两句话，成功标准通常 2–4 条，避免提前生成研究正文，但不允许省略必填字段或用户约束。严格 JSON Schema、供应商响应修复和本地契约验证全部保留，不使用残缺计划继续执行，不自动更换模型或修改 API 配置。

验证：E 盘 `langchain` 环境后端 **194 passed**、Ruff 通过。新增 `backend/tests/test_structured_output_recovery.py` 的 7 项回归覆盖“截断→单字符→有效 JSON”、完整失败链、空正文、同类失败停止、格式纠错后遇到截断等场景；本轮未发起真实付费模型请求，不能保证供应商后续一定返回正确结果。重启后端及 Studio，再新建 Run 验证；`.env` 无需更改。


### v5.0 修补 — 行程站点 POI 预消歧与最高分选择（2026-09-29）

此前 `map_itinerary` 在逐段查询时才解析每一端点，第 1 站无法精确匹配就会立即失败；也没有把全部站点先统一转换成高德正式 POI。此次按确认方案改为“先解析全部站点，再查询路线”，版本仍为 `5.0.0`。

- 两点路线和多站行程均通过同一候选评分器解析地点。多站行程在发出任何路径规划请求前，先完成全部 2–6 个站点的 POI 搜索、硬约束过滤和评分；任一站点没有有效候选时不开始路线查询。相同行程内完全相同的结构化站点只解析一次。
- 候选先校验城市范围、有效坐标、明确区县及完整地址；入口方向和门号冲突直接排除。用户要求场所或校园入口时，地铁站、公交站、停车场、售票处、餐饮、酒店、便利店和商店等不相符类型不参与排名。
- 有效候选按本地相关度得分选择最高者：正式名称完全一致优先；明确入口允许高德正式名称在场所与方向门之间加入园区／校区限定词；完整地址精确匹配次之；其余仅接受长度足够且名称相似度达到阈值的候选。显式区县和地址作为额外确定性加分。该分数是 AtlasFlow 本地匹配分，不是高德官方分数或概率；同分时采用高德响应顺序作为稳定决胜规则。
- 选择完成后，路线接口只使用高德返回的正式 POI 名称、POI ID 和坐标；导航链接也展示正式名称，不再使用模型原始别名。匹配分、候选列表、地址、坐标和路线数字仍不进入 RunRecord、Event、报告或 LangSmith；只保留允许的导航链接及安全状态。
- 行程若在预解析阶段失败，错误明确到“第 N 站”；已解析内容不会形成部分导航。若所有站点解析成功后某路段失败，错误明确到“第 N 段”，并只保留此前已完成路段的导航链接。

验证：E 盘 `langchain` 环境后端 **199 passed**、Ruff 通过。新增 `backend/tests/test_poi_preflight.py`，覆盖最高分胜出、正式园区入口名、错误方向／门号／交通设施排除、同分按供应商顺序、重复站点只解析一次、先解析后路线及部分路段失败。真实高德只读验证使用用户给出的四站参数：北京大学东门 → 圆明园遗址公园 → 中国国家博物馆 → 北京大学中关新园，成功生成 **3 条**相邻路段导航链接；没有输出或保存候选、坐标及 Key。

重启后端与 Studio 后创建新 Run 使用。最高分自动选择提高成功率，但不是人工确认；对于区县、地址或入口存在明确冲突的候选仍会拒绝，不能为了成功率导航到错误地点。

### v5.0 修补 — 非成稿 Agent 的精简输出契约（2026-09-29）

此前 Planner、Researcher、Critic 和 QualityGate 的提示词虽然要求简短，但结构化 Schema 仍允许单字段数千字、最多 20 条问题，请求输出预算也普遍为 4096–5000 Token，供应商因此可能返回接近报告长度的中间结果。本次将“准确精简”改为网关硬契约，大版本仍为 `5.0.0`，历史 Run 与章节不改写。

- Planner 只输出调度需要的简短理由与任务 DAG：初始上限 2400 Token，理由最多 300 字；任务标题、目标和成功标准分别受长度及条数限制，不提前撰写研究结论或行程正文。
- Researcher 初始上限 1600 Token：摘要最多 400 字，发现最多 8 条，局限最多 5 条；一条只表达一个可核验结论。动态事实的真实来源、必要数字和证据缺口不能以“精简”为由删除。
- Critic 初始上限 2200 Token，QualityGate 初始上限 1400 Token：理由只说明本次路由／验收依据，只列真正影响决定的问题与可执行建议，各类清单最多 8 条；accept 且无实际问题时使用空数组，不复述计划、研究结果或整篇报告。
- 工具阶段初始输出预算从 3000 降至 1600 Token，只允许返回下一步所需的原生 `tool_calls`，不附分析、计划或结论正文；截断时最多恢复到 3200。Planner、Researcher、Critic 与 QualityGate 的截断恢复也可临时扩大请求预算，但字段字符数和数组条数硬限制保持不变。
- Synthesizer 的首次合稿和报告修订仍保留 7000 Token，不压缩最终 Markdown 报告。内部 Pydantic 存储契约保留原有兼容上限，旧 Run 不会因本次收紧而无法读取；新模型输出同时经过 Provider JSON Schema 与本地长度校验，端点忽略 Schema 时也会被拒绝并自动要求精简重试。

验证：E 盘 `langchain` 环境后端 **201 passed**。新增测试覆盖五个阶段的独立 Token 预算、结构化字段上限、Synthesizer 详细输出预算不变，以及供应商返回 401 字摘要时本地拒绝并成功精简重试。重启后端和 Studio、创建新 Run 后生效；历史 Run 中已保存的长回复不会自动重写。

### v5.0 修补 — 推理预算耗尽与可选工具阶段降级（2026-09-29）

对应 Run `cf4a57b5-fba7-420f-9993-8c479e42e6a1`、Trace `0dbac438-b8cb-47e9-99e6-ebffa6edabbd`：4 个研究任务均已成功，累计完成 11 次工具执行并取得 36 条证据，但 Critic 的可选工具决策连续两次把全部输出预算用于模型内部推理，返回 `finish_reason=length` 且没有正文或 `tool_calls`。旧流程把这一可选补充失败当作 Critic 节点失败，导致已有研究成果无法进入正式审查。本次仍属 `5.0.0`，只追加记录，不改写历史 Run。

- OpenRouter 客户端新增阶段级 `reasoning_effort`。Planner、Researcher、Critic 的结构化决策默认使用 `low`，QualityGate 使用 `minimal`，原生工具决策使用 `minimal`；Synthesizer 仍保留 7000 Token 的完整报告预算。当前配置模型 `~deepseek/deepseek-v4-flash-latest` 的公开元数据已确认同时支持 `reasoning_effort`、工具调用和结构化输出。
- 网关识别“`finish_reason=length`、正文为空、存在 reasoning”的组合为 `REASONING_BUDGET_EXHAUSTED`，与普通正文截断分开处理。第一次出现时不再盲目把 Token 上限翻倍，而是在相同输出预算下将推理强度切为 `none` 后重试；普通正文截断仍按原规则扩大预算。
- 工具决策采用同样恢复策略：首次纯推理耗尽时从 `minimal` 切换为 `none`，连续两次仍无完整 `tool_calls` 才失败。残缺调用永远不会执行，因而不会误耗工具预算或产生伪造工具记录。
- Planner、Critic、QualityGate 与 Synthesizer 的工具阶段属于可选补充。其工具决策失败时记录 `tool_failed` 降级事件和 warning，并使用现有安全证据上下文继续进入该 Agent 的正式结构化决策或成稿；Researcher 声明的 `required_capabilities`／`capability_alternatives` 仍保持严格失败语义，不能在必需实时证据缺失时静默放行。
- 角色级工具上限与 RunPolicy 分离：Planner 仍最多执行 1 次，Critic 和 QualityGate 各最多执行 2 次；Researcher 仍受单次决策与整个 Run 的 5／20 默认预算控制。提高全局预算不会让审查阶段无边界地追加工具。
- LangSmith 的结构化恢复诊断现在记录安全的 `reasoning_effort`、finish reason、正文长度与 Token 上限，不保存内部推理正文；前端会看到可选工具失败后的降级事件，而不是整个 Critic 节点直接终止。

验证：E 盘 `langchain` 环境后端 **204 passed**、Ruff 通过。新增回归覆盖“结构化 JSON 纯推理耗尽后同预算关闭推理并成功恢复”“工具决策连续纯推理时不执行残缺调用”“Critic 可选工具失败后继续、Researcher 必需工具仍严格失败”。本轮没有重新发起付费端到端研究；需重启后端和 Studio，并创建新 Run 验证。原失败 Run 不会自动续跑或重写。

### v5.0 修补 — 官方 POI 前置解析与行程猜测循环终止（2026-09-29）

对应 Run `8dbdd8de-d700-4481-8db5-9e6427c663fd`、Trace `6270a283-26e7-40ae-a156-38cbba23f5ab`：T4 首轮先把城市错误写成 `北京市海淀区`，随后连续 10 次改写整条 `map_itinerary`，依次猜测“北京大学医院(东门)”“北京大学医院”、颐和园入口和地址，耗尽单轮工具预算后才在 Researcher 第二次尝试成功。更严重的是，旧匹配器把用户指定的“北京大学东门”接受成了“北京大学医院(东门)”，最终导航虽然成功，却违反了明确的起点约束。本次仍属 `5.0.0`，历史 Run、事件和报告不改写。

- 城市参数在 Pydantic 参数校验阶段规范到城市级。`北京市海淀区`、`北京海淀区` 等明确直辖市写法会转换为 `北京`；其他包含地级市的行政区字符串提取城市，不再把区县组合直接传给高德 `region`／`city`。
- 校门、公园门等带方向入口的地点在路线查询前优先调用高德官方输入提示接口 `/v3/assistant/inputtips`。该接口能返回普通 v5 文本检索遗漏的正式入口 POI；只有未取得可用精确候选时才回退到 `/v5/place/text`。所有站点仍必须先完成 POI 解析，之后才允许请求任何相邻路段。
- POI 评分同时校验正式名称、方向入口、区县、地址与高德类型码。没有明确要求交通或医疗设施时，地铁／公交和医院类候选不参与排名；入口前插入“医院”“物理系”等附属机构名称也不再被当成校园入口。允许的校园限定词保持窄范围白名单，不能靠名称前缀相似绕过约束。
- 用户问题明确“从某入口出发”时，工具执行前只核对第一站，并要求模型参数与该入口完全一致。场所整体、附属医院、其他门和同名地铁站都会在 HTTP 请求和工具预算扣除前拒绝。
- `map_itinerary` 继续在单次工具内部完成 2–6 个站点的全量预解析，但现在每个 Agent 工具阶段最多实际执行一次。确定性 POI 失败后直接保留缺口或进入既有 Researcher 重试，不再让模型在同一阶段连续猜 10 组地点；普通网络瞬时错误仍由工具注册表内部的有限重试处理。
- 高德候选、坐标、匹配分和 API Key 仍不进入 RunRecord、事件、报告或 LangSmith。持久化内容仍只有安全状态、失败说明和导航链接；成功链接使用高德返回的正式 POI 名称。

验证：E 盘 `langchain` 环境后端 **207 passed**、Ruff 通过。真实高德只读冒烟测试故意使用 `city="北京市海淀区"`，一次完成 **5 个站点、4 段路线**，正式站点为 `北京大学(东门) → 颐和园 → 圆明园遗址公园 → 清华大学 → 北京中关村北京大学逸扉酒店`，未再选择北京大学医院。重启后端和 Studio、创建新 Run 后生效；旧 Run 中错误的导航链接不会自动替换。

### v5.0 增量 — 学术型报告 Skill、编号引用与确定性文献表（2026-09-30）

本次为 Synthesizer 增加项目内置、可随代码版本管理的 `academic-report` Skill，大版本仍为 `5.0.0`。Skill 位于 `backend/src/atlasflow/skills/academic-report/`，由 `SKILL.md` 规定报告结构、详实程度和写作边界，由 `references/citation-contract.md` 单独规定引用协议。它只约束 AtlasFlow 运行时成稿，不会修改 Codex 的个人 Skill，也不要求额外 API Key。后续如需增加旅游、金融或技术尽调等报告模板，可在同一目录下扩展领域规则，而不必把全部要求继续堆入网关代码。

- **更完整的正文**：Synthesizer 不再只接收末尾少量摘要，而会获得去重后的完整证据目录和更长证据摘录；报告按研究问题组织分析章节，并固定包含 `摘要`、`结论`、`局限`、`参考文献`。多任务且证据充分时，正文通常要求约 1500–3500 个中文字符，但不允许用重复内容凑长度，也不允许把 Planner 的任务列表或 Agent 执行日志写进最终报告。
- **论文式编号引用**：正文事实使用 `[1]`、`[1][3]` 等编号就近引用，不再显示 `[task:T1]`，也不在正文散落裸链接或 Markdown 链接。编号只能来自系统生成的引用目录；模型没有证据时必须明确标注限制，不能自行补 URL、作者、日期或来源。
- **确定性参考文献**：最终 `参考文献` 不是直接信任模型生成，而是由程序根据正文实际使用的编号和已核验 Evidence／导航记录重建。网页来源输出为带访问链接的 `[EB/OL]` 条目，地图导航输出为 `[地图/OL]` 条目；未被正文引用的来源不会为了显得丰富而塞入文献表，未知编号也不会伪造条目。
- **更严格的 QualityGate**：新增章节完整性、最小详实度、编号是否存在、正文引用与文献表一一对应、裸链接、内部任务标记和证据支持关系检查。系统仍区分“引用格式正确”和“证据真的支持该陈述”，仅有合法编号不能替代语义核验。
- **报告阅读与下载样式**：网页报告和导出的 HTML 对编号文献使用悬挂缩进、较紧凑字号和稳定换行；警告文案及页脚统一使用“参考文献”术语。原有 Markdown／HTML 下载能力保留，下载内容与页面正文使用同一份经校验报告。

验证：`academic-report` Skill 通过官方结构校验；E 盘 `langchain` 环境后端 **211 passed**、Ruff 通过，前端 `typecheck` 与报告专项测试通过。未将已有 Run 的查询、研究结果和证据发送给 OpenRouter 做付费冒烟测试，因为这属于向外部供应商发送现有运行数据，需要单独明确授权；本地确定性测试不受影响。重启后端和 Studio、创建新 Run 后即可看到新版成稿，历史报告不会自动重写。

<a id="v060"></a>

## v0.6.0 — RAG Knowledge Platform（2026-10-04）

本节只追加 `v0.6.0` 的增量，不重写上文的历史版本记录。本版将早期的内存 RAG 样例扩展为独立知识平台，并通过稳定的领域接口为后续 LLMWiki 留出扩展位；当前不实现 LLMWiki，也不把 AtlasFlow 项目文档或历史 Run 自动当作语料。

### 架构与数据流

- 新增 `atlasflow.knowledge` 边界，分为 Domain Model、Application Service、Repository／Blob Store／Parser Port 和 Infrastructure Adapter。Agent、HTTP API、后台 Worker 和未来 LLMWiki 共用同一平台接口，不直接依赖 PostgreSQL 表。
- 持久化使用 PostgreSQL + pgvector，通过 Alembic 管理 Schema；原文件使用受路径约束的本地 Blob Store。默认数据目录为 `E:/codex/agent/data/knowledge` 和 `E:/codex/agent/data/postgres`，处理中间文件位于 `E:/codex/tmp/atlasflow-rag`，均不占用 C 盘。
- 文档使用不可变 `DocumentVersion`、内容哈希去重和可切换 `IndexGeneration`。重复内容不重复建索引；新版本只在完成解析、切块、Embedding 和索引发布后成为当前版本，检索不会读到半成品。
- 异步任务保留 `queued → parsing → chunking → embedding → indexing → completed/failed` 状态和错误诊断，支持失败后显式重试。空库会创建 `user-default` 空间，不会隐式注入示例文档。

### 解析、切块与检索

- 首版支持 PDF、Markdown／MDX、HTML 和纯文本。Parser 保留标题路径、页码、块顺序、列表、表格、引用与代码块等来源定位，避免只存一段不可回溯的纯文本。
- 切块采用 parent-child 结构：子块用于精确检索，上下文组装时回到父块并去重；中文词法索引使用 `jieba`，保留确定性 fallback。
- 查询链路为保守规范化 → 词法检索 → 向量检索 → RRF 融合 → Rerank → 父块上下文组装 → 充分性决策。结果明确返回 `sufficient`、`partial` 或 `insufficient`，不把无关语料包装成答案。
- 词法分数、向量分数、融合排名、重排序结果、选中原因和决策结果都进入可观测 Trace，便于在 LangSmith 中分段定位检索失真。Rerank 不可用时会显式标注 fallback，不伪装为正常重排序。

### Agent、API 与前端

- Planner 只在结构化任务中声明 `knowledge_search` capability，不执行检索。Researcher 是主要调用者；Critic 和 QualityGate 可在审查缺口时有限调用；Synthesizer 不自行检索，只消费已经进入 Evidence 契约的结果。
- 新增 `/api/v1/knowledge` API，覆盖知识空间、文本导入、25 MB 文件上传、文档版本、任务状态／重试、带 Filter DSL 的检索与调试检索。当前 HTTP 端仅提供可恢复的 archive；由于尚未实现真实身份认证，不暴露不可恢复的物理删除端点。
- 新增固定侧边栏的「知识库」工作台，包含空间／文档台账、导入任务和检索实验室。检索实验室用分段分数轨迹展示 lexical、vector、fusion 和 rerank，支持动态轮询任务状态；archive 等易误触操作保留二次确认。

### 首批语料清单

`corpus_manifests/llm-engineering-v1.yaml` 记录 18 个候选来源，覆盖 LLM 基础、训练／对齐、推理、主流模型、RAG 和 Agent，并为每个来源记录来源 URI、内容类型、优先级、许可及再分发策略。`auto_download` 被强制为 `false`：清单用于审核和后续手工获取，不会在启动或测试时自动下载第三方 PDF／仓库。原文、切块、向量和未来下载目录都已加入 Git 忽略规则。

### 运行方式

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:DOCKER_CONFIG = 'E:\codex\agent\.docker'

# 1. 在已有 langchain 环境中刷新 v0.6.0 依赖与命令入口
E:\conda_envs\langchain\python.exe -m pip install --cache-dir E:\codex\cache\pip -e .

# 2. 启动 pgvector PostgreSQL
docker-compose up -d postgres

# 3. 升级数据库
E:\conda_envs\langchain\python.exe -m alembic upgrade head

# 4. 启动 API（.env 中 KNOWLEDGE_WORKER_ENABLED=true 时同进程处理导入任务）
E:\conda_envs\langchain\python.exe -m uvicorn atlasflow.main:app --app-dir backend/src --reload

# 5. 启动前端（新终端）
Set-Location E:\codex\agent\frontend
npm run dev
```

若关闭 API 内置 Worker，可在独立终端运行 `E:\conda_envs\langchain\python.exe -m atlasflow.knowledge.worker`。语料清单可用 `E:\conda_envs\langchain\python.exe -m atlasflow.knowledge.cli validate corpus_manifests/llm-engineering-v1.yaml` 校验。

验证边界：自动化测试使用内存 Repository，不调用付费 LLM，也不下载外部语料。本轮后端 **218 passed**（另有 1 条第三方弃用警告）、Ruff 通过、前端 TypeScript 及 flow／event／report 专项校验通过，manifest 与 Alembic head 验证通过。Next.js 生产构建在当前环境中长时间无任何新输出，已主动终止，不记为通过。当前本机 Docker daemon 未运行，因此 PostgreSQL／pgvector 真实集成尚未完成冒烟验证，不把内存测试冒充为持久化验证。

### v0.6.0 启动故障补记（2026-10-05）

后端在 `knowledge.start()` 阶段出现 `ConnectionRefusedError: 127.0.0.1:5432`，原因是 PostgreSQL 尚未运行；这与 OpenRouter API 和 Agent 逻辑无关。当前安装的是独立 `docker-compose.exe`，`docker compose` 子命令不可用；Docker CLI 存在但 Docker Engine 未启动。已确认 Docker Desktop 的 WSL 数据盘文件位于 `E:\Docker\data\DockerDesktopWSL\disk\docker_data.vhdx`。启动前仍应检查 Docker Desktop 自身的其他缓存／日志位置符合 E 盘约束。

上一节的 API 命令误用了模块中不存在的 `atlasflow.main:app`。实际入口是 `create_app` 工厂，必须带 `--factory`；`-8000` 也不是有效的端口参数，正确写法是 `--port 8000`。在 Docker Desktop 已启动且确认数据位置后，按以下顺序执行：

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:DOCKER_CONFIG = 'E:\codex\agent\.docker'

docker-compose up -d postgres
docker-compose ps
E:\conda_envs\langchain\python.exe -m alembic upgrade head
E:\conda_envs\langchain\python.exe -m uvicorn atlasflow.main:create_app --factory --app-dir backend/src --reload --port 8000
```

如果 `docker-compose up` 报 `npipe:////./pipe/docker_engine` 不存在，说明 Docker Desktop／Engine 还未启动；如果 PostgreSQL 已运行但提示 `knowledge_spaces` 表不存在，则先执行上面的 Alembic 迁移。后端现在会对数据库连接失败给出 `KNOWLEDGE_DATABASE_UNAVAILABLE` 提示。数据库连接正常后，再打开 `http://127.0.0.1:8000/docs` 验证 API。

### v0.6.0 Planner 供应商错误诊断补记（2026-10-05）

Planner 首次请求 OpenRouter 时可能收到 HTTP 400，页面先前只显示 `Provider returned error`。这是模型请求被 OpenRouter／上游供应商拒绝，发生在工具和 RAG 执行之前；仅凭该句不能断定是模型、JSON Schema、参数组合还是供应商的瞬时故障。现在错误处理会在响应提供时提取供应商名称、错误码及嵌套错误信息，同时限制长度并脱敏，不保存或展示原始响应。重启后端后重新运行，若仍失败，查看新的「节点失败」错误详情；没有嵌套详情时还需借助 OpenRouter 供应商侧诊断。此修改只改善定位，不改变请求参数或自动切换模型。

### v0.6.0 原始 LLM 语料实际入库补记（2026-10-05）

此前的 18 条语料清单只有来源元数据，**没有下载或导入原文**；本次补齐了这一步。`scripts/import_llm_corpus.py` 是显式执行的导入器：按清单从 Springer、arXiv、Hugging Face 和 Microsoft 官方来源下载原文，检查 PDF／Markdown 类型和 25 MB 上限，记录 SHA-256、来源 URI、版权策略及导入任务；然后依次解析、切块、调用已配置的 OpenRouter Embedding 并发布 PostgreSQL 索引。导入器按指定任务原子认领，能够与运行中的后端 Worker 共存；同一内容再次执行会识别已发布版本，不重复写入索引。`auto_download: false` **仍然有效**，后端启动不会自动下载或注入语料。

原文、Blob 和导入账本均保存在 `E:\codex\agent\data\knowledge\`，不提交 Git；本地账本为 `data/knowledge/corpus/llm-engineering-v1/import-ledger.json`。这意味着新机器克隆仓库后需要显式运行以下命令，不应把仅有 YAML 清单理解为知识库已填充：

```powershell
Set-Location E:\codex\agent
$env:PYTHONPATH = 'E:\codex\agent\backend\src'
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:PYTHONPYCACHEPREFIX = 'E:\codex\agent\.pycache'
E:\conda_envs\langchain\python.exe scripts/import_llm_corpus.py --download
E:\conda_envs\langchain\python.exe scripts/import_llm_corpus.py --ingest
```

也可以追加 `--source react` 等参数只处理指定来源。执行前先启动 PostgreSQL 并完成 Alembic 迁移；入库会调用外部 Embedding API，下载和索引只在显式运行命令时发生。由于部分论文仅用于本机私有索引，原始 PDF 和向量不会推送至公开仓库。

本机验收：`llm-engineering-v1` 空间已发布清单中的 **18／18 份**原文（论文 12、官方 Markdown 6），共 **509 个可检索子块，509 个子块有向量**；全部文档和版本状态分别为 `active`、`ready`。通过实际 API 查询，RAG、LoRA、KV Cache、DeepSeek-V3、Continuous Batching 和 ReAct 问题的首个命中均为对应原文，决策为 `sufficient`，无向量／Rerank 降级。原有 `user-default` 空间及用户导入的 `python.md` 未被替换。导入时修复了 PDF 提取文本含 `NUL` 字符导致 PostgreSQL 拒绝写入的问题，并在原版本上重试成功；部分 PDF 的 pypdf 仍提示缺少可选 `fontTools` 字体解析依赖，个别复杂字体的文本保真度需后续逐页抽检。

回归验证：E 盘 `langchain` 环境下后端 **224 passed**（1 条第三方弃用警告），Ruff 全量检查通过；实际检索通过运行中的 HTTP API 验证。语料来源检查和实际入库属于显式联网操作，不是离线单元测试的一部分。

使用时在知识库页面选择 `llm-engineering-v1` 空间；直接调用 `knowledge_search` 工具时也可以传 `space: "llm-engineering-v1"`。当前 Agent 工具若不传 `space`，仍会使用 `.env` 中 `KNOWLEDGE_DEFAULT_SPACE` 指定的默认空间（目前为 `user-default`），不会自动跨空间检索或暗中把本语料加入每个研究任务。若希望 Agent 默认搜索这批原文，应由用户在 `.env` 中显式设置 `KNOWLEDGE_DEFAULT_SPACE=llm-engineering-v1` 并重启后端；不改动现有用户环境配置。

### v0.6.0 每次 Run 选择知识空间补记（2026-10-08）

针对模型把不存在的 `space="default"` 传给 `knowledge_search`、省略空间后又误搜 `user-default/python.md` 的问题，新增 Run 级知识库策略。创建研究页现在可选“**不使用知识库**”（新页面默认）或某个实际存在的知识空间；运行详情显示本次选择。选择只对新 Run 生效，历史 Run 与当前执行中的计划不会被改写。知识库页面的检索实验台仍独立选择空间。

- `RunPolicy.knowledge_mode` 支持 `off`、`selected`、`default`；`selected` 必须同时提供 `knowledge_space`，后端在创建/重新运行前验证空间存在。旧 API 调用与历史记录缺少新字段时仍按 `default` 兼容，不破坏已有用法。
- `off` 时 Planner/Critic 的能力目录不包含 `knowledge_search`，工具阶段也不向模型提供该工具；即使模型违规返回工具名，也会在执行前拒绝。`selected` 时工具阶段从 RunPolicy 强制写入所选空间，并从模型可见参数中移除 `space`，避免模型自行编造空间名。兼容模式 `default` 忽略模型传入的空间别名，始终使用 `.env` 的默认空间。
- `knowledge_search` 成功记录现在提供简短结果摘要，不再在前端只显示“无结果摘要”；摘要不替代 Evidence 和来源明细。空间选择不会强制使用 RAG：任务是否需要 `knowledge_search` 仍由 Planner 判断，工具执行需符合能力契约与预算。

本次没有修改 `.env` 或已有知识库数据。验证：后端 **230 passed**、Ruff 通过、前端 `typecheck` 通过；新增测试覆盖关闭知识库、锁定选定空间、纠正 `default` 别名和不存在空间的 API 校验。更新前后端后，在创建研究页选 `llm-engineering-v1` 即可只检索该空间；若完全不需要 RAG，保持“不使用知识库”。

### v0.6.0 本机环境配置整理补记（2026-10-08）

本机 `.env` 已按当前代码读取的配置项整理，保留原有 LLM、Embedding、Rerank、LangSmith 和高德 Key，不记录或提交其值。将旧的 `DATABASE_URL` 原值迁移为知识库实际读取的 `KNOWLEDGE_DATABASE_URL`；Run/事件仍使用 `DATABASE_PATH=E:/codex/agent/data/atlasflow.sqlite3`，知识库文件和工作目录也显式位于 E 盘。删除未被当前运行路径读取的 `APP_ENV`、`LOG_LEVEL`、`REDIS_URL`、`MAX_AGENT_ITERATIONS`、`RAG_TOP_K`，以及空置的搜索模型与搜索 Key；搜索继续按现有逻辑复用主模型与 Key。

已确认 PostgreSQL 中存在 `llm-engineering-v1`，因此本机 `KNOWLEDGE_DEFAULT_SPACE` 设为该空间，供未显式选择空间的旧 API/Studio Run 使用；网页新 Run 的空间选择或“不使用知识库”优先于该默认值。`.env.example` 同步补充了参数分组、隐私提示与存储路径说明。修改 `.env` 后需要重启后端；这次只验证了配置解析及知识空间连接，未发起付费模型或工具调用。

### v0.6.0 检索实验台与单次 RAG 调参补记（2026-10-08）

知识库“检索实验台”调整为选择空间、提出问题、设定参数、观察阶段结果的流程；知识空间选择在知识库页面间保留，文档页可直接进入检索实验台。可调返回片段数、融合候选数、关键词/语义相对权重、是否重排、重排候选数及 RRF 常数。页面在参数或问题修改后明确提示旧结果尚未刷新，展示双路召回、融合、重排和最终返回的数量，以及原文片段和各项分数。分数条各自按本指标归一显示，不能把 BM25、向量、RRF、Rerank 数值直接横向比较。

`POST /api/v1/knowledge/search/debug` 新增可选的 `tuning` 对象，兼容未传参数的旧请求；参数只作用于该次检索，不修改空间索引或 Agent 的默认检索配置。后端现在真正应用 `candidate_limit`，可跳过 Rerank，并在 Trace 中返回生效参数、重排候选数和降级信息；对越界值及相互矛盾的上限返回 422。关闭或失败重排时，充分性判断使用归一化融合分，避免直接把未归一的 RRF 分数与重排阈值比较。候选上限是在召回和打分之后应用的，当前仍会扫描该空间可检索的分块；它主要控制进入重排与最终返回的规模，并非向量索引层的性能开关。

回归验证：E 盘 `langchain` 环境下后端 **234 passed**（1 条第三方弃用警告），Ruff、前端 TypeScript、现有 UI 静态检查和 CSS 解析通过。当前 Windows 环境中的 Next.js 生产构建长时间停留在编译阶段、无进一步输出，已主动停止；尚未完成浏览器可视化验收或真实 OpenRouter 调用。

### v0.6.0 RAG 解析与父子切块统一补记（2026-10-11）

PDF、Markdown、HTML、TXT 现使用统一的解析块来源定位与有上限的父子切块规则；长段落继续拆分，PDF 父块不再跨页。检索结果保留实际命中的 child，仅附加有界邻近上下文。解析器／切块器版本升级为 v2；历史索引不自动改写。`scripts/reindex_knowledge.py` 默认只预览，显式 `--enqueue` 后由 Worker 重建；成功后才切换发布版本。重建会调用 Embedding API，可能产生费用。前后差异、迁移命令和验收清单如下：

本文记录管线从 `structured-parser-v1`／`parent-child-v1` 到 v2 的变化。适用于 PDF、Markdown、HTML 和 TXT；升级代码不会自动改写已发布的索引。

#### 前后变化

| 环节 | 旧版 | 本版 |
| --- | --- | --- |
| 文本预处理 | PDF 单独删除 NUL | 四种格式共用换行及 NUL 规范化；不删除参考文献或正文词语 |
| 来源定位 | PDF 有页码，其余格式缺少源位置 | PDF 有页码及页内字符范围，MD/HTML 有行范围，TXT 有全文字符范围；全部有块顺序号 |
| 父块组织 | 按标题路径全局聚合，重复标题可能混合前后内容，PDF 也可能跨页 | 按原文顺序成组；遇到标题或 PDF 页边界就断开 |
| 长段落 | 只在解析块之间切分，单个长块可超过上限 | 超长块继续拆分；默认 parent ≤ 1200、child ≤ 350 个估算 token |
| 父子关系 | 检索到 child 后，用整个 parent 替换返回正文 | child 仍用于打分、定位和引用；`parent_id` 保留，仅附加最多 500 token 邻近上下文 |
| 版本去重 | 同一 SHA-256 原文跳过重建，即使规则已升级 | 去重检查发布版本的解析器和切块器版本；旧文档可显式重建 |

#### 执行流程与代码

`ParserRegistry` 选择格式解析器 → 统一的 `ParsedBlock`（正文、类型、标题路径、顺序、源位置）→ `StructuralChunker` 按章节和页顺序分组 → 超长块继续拆分 → 有界 parent 和其内的 child → child 执行 Embedding、双路检索与重排 → `ContextAssembler` 保留 child，只补充有界上下文。

这仍是父子切块：parent 与 child 都持久化，每个 child 的 `parent_id` 指向包含它的 parent，只有 child 建立可检索向量；需要上下文时再从其 parent 按命中位置截取。改变的是**返回证据不再把 child 替换为 parent**。PDF 目前不推断标题层级，而是按页、段落及长度切块。

- `backend/src/atlasflow/knowledge/ingestion/parsers/`：各格式解析及共用规范化。
- `backend/src/atlasflow/knowledge/ingestion/chunker.py`：顺序分组、硬上限、父子关联、`source_spans`。
- `backend/src/atlasflow/knowledge/retrieval/context_assembler.py`：邻近上下文，不替换 child。
- `backend/src/atlasflow/knowledge/application/platform.py`：版本化摄取、同原文去重和显式重建。
- `backend/src/atlasflow/tools/knowledge_search.py`：Evidence 仍来自 child；上下文只是辅助元数据。

`token_count` 用项目内正则估算，不是具体模型的 tokenizer。上限保证估算值；需要严格模型窗口时，应另接相应 tokenizer。`source_spans.block_char_start/end` 指原 `ParsedBlock.text` 内字符范围；PDF 的 `source_char_start/end` 是页内位置，TXT 是全文位置。字符范围不是 PDF 坐标。扫描 PDF、复杂字体、表格、页眉页脚及参考文献仍需抽检，本版不静默删除这些内容。

#### 旧索引重建

旧索引仍可检索，但原有分块边界不会随代码自动变化。先启动 PostgreSQL 与后端，再在 E 盘 `langchain` 环境预览某个知识空间：

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1
```

确认后显式排队：

```powershell
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1 --enqueue
```

对其他空间重复指定 `--space`。默认预览只调用 GET；`--enqueue` 创建新版本和 Job，后台 Worker 才会重新解析、切块并调用 Embedding API，可能产生费用。新版本成功后才更新 `current_version_id`；失败时旧版继续发布。脚本不下载、覆盖或删除原文，也不主动执行排队 Job。单篇可用 `POST /api/v1/knowledge/documents/{document_id}/reindex`。

重新上传同一原文不再仅因 SHA-256 相同而跳过 v1→v2；已有同原文 v2 任务排队或运行时会复用该任务。历史版本和 Blob 继续保留，因此重建后版本与 Chunk 总数可能增加。检查实际生效版本应看文档 `current_version_id` 对应的 `parser_version`、`chunker_version`、`status`，不能只看表中是否存在 v2 Chunk。

#### 验收

1. PDF、MD、HTML、TXT 的长内容均产出有上限的 child/parent 和可追溯 `source_spans`；PDF parent 不跨页。
2. 检索实验台返回的原文、分数和页码来自 child；邻近上下文只能辅助理解，不作为独立引用。
3. 重建排队期间旧版仍可检索；成功后生效版本为 `structured-parser-v2`／`parent-child-v2`。

离线回归位于 `backend/tests/test_knowledge_platform.py`；真实 PDF 的公式、字体和表格保真度仍需人工抽检。

### v0.6.0 知识空间版本快照与整体切换补记（2026-10-11）

当前实现是显式保存完整的 `KnowledgeSpaceVersion` 快照：含空间索引世代、每份文档的发布版本 ID 与归档状态，以及最多 500 字的空间版本说明。知识空间页面集中展示空间版本台账，不在文档行提供切换按钮。说明属于可编辑的空间版本元数据，不会改变原文、Chunk 或 Embedding，也不是逐次修改的审计日志。

先运行 `python -m alembic upgrade head` 创建 `knowledge_space_versions` 表。`POST /api/v1/knowledge/spaces/{space}/versions` 保存当前空间快照；`GET` 同路径列出快照；`PUT /api/v1/knowledge/spaces/{space}/versions/{version_id}/note` 修改说明；`POST /api/v1/knowledge/spaces/{space}/versions/{version_id}/activate` 将**整个空间**切换到目标快照。切换在一个 PostgreSQL 事务内恢复所有文档的检索指针、归档状态和索引世代。目标快照之后新增的文档会归档，但原文和索引不删除；切回较新的空间版本可重新发布。若存在待处理任务、快照引用的文档已永久删除、目标版本缺失或 child 索引不完整，则拒绝切换。摄取、归档或新增文档会把空间标为“未保存变更”；需在任务结束后保存新快照。旧数据不会自动生成历史快照，也没有对用户数据库执行切换。

#### 已有文档版本的空间快照回填（2026-10-11）

这次使用 `scripts/backfill_space_versions.py` 从数据库保留的文档版本和完整 child 索引重建了三个空间快照，并已写入现有 PostgreSQL 的 `knowledge_space_versions` 表：`llm-engineering-v1` v1、v2 各含 18 份文档，当前指向 v2；`user-default` v1 含 1 份文档，当前指向 v1。回填没有重新解析、切块或调用 Embedding，也没有修改文档当前版本及索引世代。每条说明明确标记为“事后重建”，`created_at` 是回填时间，不冒充原始索引日期。回填只依据目前仍保留的文档及版本，无法证明已删除文档或历史归档状态；若将来遇到版本波次不齐、索引缺失等情况，脚本会拒绝推断。

脚本默认只读预览；必须显式传入 `--apply` 才会在事务中写入。已有空间版本时会拒绝重复回填。

### v0.6.0 论文 PDF 切块与双路召回补记（2026-10-12）

本次将 PDF 解析器升级为 `structured-parser-v3`：对可从文本层可靠识别的独立标题建立章节路径，并给摘要、正文、参考文献区段标记 `section_role`；跨页保留章节路径，仍保留原文、页码和字符定位。参考文献没有被删除，也没有被无条件排除检索。结构识别是保守的文本规则，扫描件、复杂多栏及字体编码问题仍需人工抽检；已有 PDF v2 索引不会自动变化。父子切块版本仍为 `parent-child-v2`，child 继续作为引用依据，父块只供邻近上下文。

检索现在让 BM25 与向量两路各取最多 `candidate_limit` 个正分结果，取并集后按 RRF 融合，再截取融合候选及重排候选；缺失、零分或失败的路不再凭任意名次给所有块加分。关闭某一路时不会执行该路打分或调用 Embedding。页面“关键词／向量候选”计数现在表示两路实际进入融合的 Top-K 数量，而不是全空间的正分块总量。BM25 与余弦目前仍在应用进程中对已发布 child 计算；数据库内 ANN／FTS Top-K 下推尚未实现，不能把本次改动理解为大库性能优化。融合分仍是 RRF 名次值，重排分仍是模型原始分，`min_relevance` 尚未经过数据集校准。

只读预览当前空间需要重建的论文 PDF（2026-10-12 在 `llm-engineering-v1` 预览为 12 份）；**本次没有排队或执行重建，也没有调用 Embedding**：

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1 --mime-type application/pdf
```

确认并重启后端使新代码生效后，才可在同一命令末尾加 `--enqueue`。这会创建新文档版本并调用 Embedding，可能产生费用；完成前旧版继续发布。全部 Job 完成后再保存新的**整个空间**版本快照，原空间 v1/v2 快照不被覆盖。

### v0.6.0 整空间版本语义更正（2026-10-12）

上方两处 PDF-only 重建说明仅记录了最初的局部方案，**不作为当前操作指令**。版本迭代的单位是整个知识空间：`structured-parser-v3` 对所有格式统一生效，PDF 才额外获得章节及区段角色。正式发布下一版空间快照前，该空间内所有 active 文档都应完成新管线重建；不能将“12 份 PDF 更新、其余 6 份沿用旧索引”称为完整的空间升级。当前操作指令请以 `instructions.md` 第 11 节为准：不使用 `--mime-type`，默认预览整个空间，显式 `--enqueue` 后用 `--verify` 验收，再保存空间版本。原 v1/v2 快照保留，本次没有执行数据库重建。

已知限制：目前 Worker 会在每份文档重建成功时单独更新 `current_version_id`，所以重建期间检索可能暂时混用新旧文档。空间快照只在全部验证通过后保存；这不等于“旧版持续服务、全部完成后原子切换”。后者需要整空间暂存和原子发布机制，当前尚未实现。

### v0.6.0 整空间暂存与原子发布补记（2026-10-12）

本次补齐上方更正中提到的暂存发布机制。知识库「空间版本」新增「重建整个空间」，对所有 active 文档创建新文档版本、摄取任务和独立索引世代；archived 文档的状态与版本保留在完整清单中。原始文件复用并逐份验证哈希，不重新下载。新索引全部暂存，任务逐篇完成时不会更新对外发布指针；部分失败、部分完成、全部完成待发布三个状态都继续使用旧空间检索。

全部任务完成后，「发布整个空间版本」会重新验证完整清单、文档状态、统一管线、父子索引和有效同维向量，在一个 PostgreSQL 事务中保存新空间快照并切换所有文档与世代。空间版本号此时顺序分配。发布幂等，重复请求不会产生重复快照；后来已回退时，重复提交原构建也不会暗中再次切换。旧版本、原文和索引保留，仍可整体回退。构建期间固定清单，暂停该空间导入、归档、快照保存、历史切换和并发重建；失败文档可重试，取消需等待当前运行任务结束。正常停止 Worker 时会将未完成任务重新排队；强制结束进程后的 RUNNING 任务尚无租约恢复机制。

新增 `SpaceBuildService`、`SpaceBuild` 和共享发布校验；API 提供 `/spaces/{space}/reindex`、`/spaces/{space}/builds` 及指定构建的 `/publish`、`/cancel`。`scripts/reindex_knowledge.py` 默认预览整个空间，`--enqueue --note ...` 创建构建，`--build <id> --verify` 读取状态，`--build <id> --publish` 整体发布，`--build <id> --cancel` 取消。前端沿用现有台账样式，集中显示进度、失败原因、重试和发布确认；修复了刷新时相同 React key 导致重复区域的问题。所有带注释命令见 `instructions.md` 第 11 节；Python 类／函数与运行流程补充在本地 `docs/rag-python-code-guide.md`、`docs/rag-end-to-end.md`。

数据库迁移 `0003_space_builds` 已在现有 `atlasflow` 数据库执行，新增 `knowledge_space_builds` 表；没有新建生产数据库，也没有排队真实语料重建或调用外部模型。核验 `llm-engineering-v1` 仍为 18 份文档、空间 v1/v2 且当前 v2；`user-default` 仍为 1 份文档、当前 v1；两个空间的构建记录均为 0。PostgreSQL 数据目录挂载在 E 盘。

验证：后端 **271 passed、1 skipped**（真实 PostgreSQL 测试默认需显式启用），Ruff 和前端 typecheck 通过；真实 PostgreSQL 在独立临时 schema 的事务测试 **1 passed**，测试 schema 已清理。浏览器使用拦截 API 样例验证了重建确认、构建期间锁定、失败重试、发布确认、刷新不重复区域和移动端宽度，截图与隔离配置位于 `build/knowledge-browser`（E 盘）。

### v0.6.0 前端工作台与交互层重构补记（2026-10-13）

本次保留品牌标识、五项主导航、既有路由与后端接口，重构工作台、运行记录、运行详情、知识库、检索实验台、统计与设置页面。以中性石墨色、青绿主操作、自托管Geist与系统中文无衬线字体统一排版；减少卡片嵌套，以分隔、留白与主次栏组织信息。桌面固定侧栏，移动端五项底部导航；浅色/深色主题统一作用于全页面，选择保存在当前浏览器。静态界面文案使用准确的技术表述，中英文相邻不额外插入空格；原始报告、查询、模型输出和文档内容不被批量改写。

研究目标改为独立输入编辑器，提供4000字符限制、实时计数、错误反馈与清晰焦点。编排策略采用分段选择，知识空间、任务数量、补充策略和工具预算直接可见，参数修改会继承当前策略并切换至自定义；自动审批与提交集中放置。知识库的版本说明、整空间重建和快照保存常驻，历史版本通过明确按钮打开抽屉；文件导入使用单一选择面板。检索实验台常驻展示六组调节控件及每条证据的四项原始评分，不用不同量纲的进度条暗示可直接比较；完整原文通过文字按钮查看。

运行详情优先呈现进度、真实经过时间与质量：有报告的终态默认进入报告，失败默认进入事件；手动选择的标签不会被实时刷新覆盖。保留阶段回环、补充任务、计划版本、TaskDAG、反馈路径和完整路由记录；TaskDAG连线使用左右端口，节点文字提高可读性。事件错误原因直接展示，审批能力目录和历史版本使用明确入口。完整诊断、Provider记录及报告移动端目录仍按需展开，但不再把主要配置放进多层折叠箭头。确认弹窗与抽屉支持键盘焦点约束、Escape退出及焦点恢复。

同时修正具名工具SSE事件监听、统计质量分为空时的报错、统计/配置读取的过期响应处理，以及深色报告强调文字、表格和计划版本按钮的对比度。配置页区分“凭据已配置”和“连接已验证”，不添加后端没有返回的连接状态。保留Markdown/HTML下载、独立报告打印、导航与来源链接。没有修改Agent编排、工具预算默认值、RAG算法或数据库数据，也没有发布或切换任何真实知识空间版本。

验证使用前端typecheck、拓扑/事件/报告离线检查、生产构建，以及隔离浏览器的首页输入、运行确认、整空间构建与检索交互回归。新增 `frontend/scripts/verify-design-browser.mjs` 和 `frontend/scripts/verify-run-ui-browser.mjs`，更新知识库及报告浏览器脚本；浏览器拦截API使用本地样例，不触发真实模型、导入或数据库写入。截图、隔离配置、下载和打印测试产物位于E盘 `build/`，自托管依赖与缓存也保存在E盘。本地审视记录在 `docs/frontend-design-audit-2026-10-13.md`，该目录继续按既有规则不进入Git。

### v0.6.0 事件配色回归与召回滑块方向修正（2026-10-13）

修复上一轮重构遗漏的事件样式绑定：每个事件类型的颜色映射原本仍保留，但浅色标签与时间线圆点没有消费对应CSS变量。现在标签底色、可读文字色与圆点恢复独立的类型配色，深色文字使用对应明度调整；不只依赖颜色识别事件，中文类型标签继续保留。事件检查补充最终样式绑定断言，浏览器回归验证实际颜色、浅色/深色和移动布局；对比度验收支持浏览器返回的`color(srgb ...)`格式。

召回策略保留原版原生滑块与单行百分比显示，按用户指定方向改为**向右增加关键词权重，向左增加语义权重**。默认仍为50%/50%；滑块绑定关键词占比，关键词占比20%对应`lexical_weight=0.4`、`vector_weight=1.6`，80%则相反。后端的关键词/语义权重含义和RRF计算不变。更新读屏说明及回归，验证20%/80%、80%/20%、0%/100%、100%/0%、50%/50%的显示与实际提交字段，以及原生左右方向键；参数调整与恢复默认都不会隐式发起检索。

本次未添加版本删除功能，未改动数据库。空间管理两项操作的区别是：**重建整个空间**重新解析、切块并生成Embedding，旧版持续检索，完成后需要确认整体发布；**保存当前空间版本**只保存现有文档清单、发布状态与索引世代的快照，不重算索引或调用模型。保存会把当前快照编号指向新记录，但检索内容不变；重建成功发布时也会自动保存新快照，无需额外再点保存。当前API未实现空间版本删除。

### v0.6.0 版本入口调整与历史版本删除补记（2026-10-13）

本次按追加要求移除主操作区的「保存当前空间版本」，主流程明确为「重建整个空间→验收→发布」。修改解析代码、切块规则或Embedding配置后，应先重启后端使其生效，再重建并发布；发布事务自动保存新空间版本，不能仅通过保存快照应用这些变更。原快照API保留兼容性，前端将它更名为历史管理中的次级「创建当前索引快照」并增加确认，说明只记录现有索引。当前发布状态已有快照时禁止重复创建；只有未保存且有效文档均已准备完成的空闲状态才允许。首次重建前仍由后端自动保存完整基线。

历史抽屉新增「删除版本」及危险操作确认。后端提供 `DELETE /api/v1/knowledge/spaces/{space}/versions/{version_id}`，成功返回200与带 `deleted_at` 的版本记录。删除采用逻辑删除：从公开历史列表移除，不能再通过公开接口切换或修改该快照；内部审计记录、原文、文档处理版本、切块、向量及索引世代均保留，当前检索状态不变。当前版本不能删除；存在活动构建或摄取任务时禁止新的删除。已删除版本重复请求返回原记录，后续忙碌状态也不产生重复写入。完成/取消构建保留历史引用，已发布构建的重复发布返回原记录且不会复活删除版本或再次切换索引；版本编号计入已删除记录，不回收、不复用。

PostgreSQL删除与激活共用空间行锁，事务内复核归属、当前指针、构建及任务状态；备注与激活操作也重新读取记录，防止陈旧对象覆盖删除标记。跨空间或已删除版本的公开激活/备注请求返回404，当前版本及忙碌空间删除返回409。`deleted_at` 位于现有 `knowledge_space_versions.payload` 的JSONB中，旧记录缺省为None，**没有新增表、数据库或迁移**；本次没有替用户删除任何实际版本。

验证：后端 **292 passed、1 skipped**，Ruff和前端typecheck通过；真实PostgreSQL的独立临时schema回归 **1 passed**，验证数据保留、重复发布、编号递增与并发删除/激活，测试schema已清理。浏览器使用拦截API验证主入口移除、快照确认与重复创建保护、删除确认与取消、当前/忙碌保护、历史列表更新和原RAG比例交互；桌面、移动与深色检查通过。生产后端需重启以加载新接口；不需要执行新的数据库迁移。

### v0.6.0 中英混合查询扩展补记（2026-10-14）

此前检索仅对原问题做空白归一化，BM25和向量召回各使用一次原查询。现在在查询阶段增加可替换的`QueryExpander`接口，默认由`BilingualQueryExpander`基于中英术语与缩写表生成有限的混合查询。原始问题始终置首，不被翻译、替换或截断；例如中文注意力问题可追加`attention mechanism`以召回英文论文。内置术语涵盖当前LLM/RAG/Agent语料，自定义JSON可按术语ID覆盖或新增映射。它是术语扩展，不是任意句子的翻译、HyDE或LLM改写；未收录的概念保持原查询，不凭空补充内容。

检索先验证空间权限，读取同一已发布索引及过滤后的child集合，再执行扩展。每条查询分别计算BM25和向量相似度；查询向量通常通过**一次Embedding批量请求**取得，不重新生成文档向量。同一路内先按名次合并变体：原始查询权重1，所有追加查询共享总权重0.5，防止扩展数量放大排名影响；再沿用原来的关键词/语义加权RRF、候选限额、原问题重排和父子上下文组装。同一child只进入候选一次，零分或不可用的召回不会得到虚假名次。展示的关键词/语义原始分取各变体的最大值，仅用于诊断，不能直接跨查询或跨量纲比较。

扩展异常时仅使用原问题并记录降级；批量Embedding失败时最多追加一次原问题Embedding回退，仍失败则保留关键词召回。空空间、无过滤命中、不存在或无权限的空间不会发出Embedding或重排请求。新增Trace字段包括是否请求/应用扩展、实际查询列表、匹配术语、扩展方法与评分口径；LangSmith保留`knowledge.query.expand`及批量向量召回子步骤，继续遵循既有内容上传开关。

配置新增`KNOWLEDGE_QUERY_EXPANSION_ENABLED=true`、`KNOWLEDGE_QUERY_EXPANSION_LIMIT=2`和可选`KNOWLEDGE_QUERY_ALIASES_PATH`。上限表示**最多追加的查询数**，允许1–4；默认扩展器目前最多生成英文/中文两种补充变体，并不保证每次用满上限。Agent工具及未覆盖参数的API查询使用服务默认；检索实验台可按单次请求开关、调整上限，并直接展示原始问题和实际变体，只有点击「运行检索」才生效。前端沿用现有实验台层级，不引入新的折叠或装饰模块。

这是查询时增强，不改变文档解析、切块、已有Embedding或空间版本；**重启后端并刷新前端即可，不需要迁移数据库或重建空间**。不增加LLM调用，查询Embedding的输入条数可能增加。自定义词表在启动时加载并校验，错误文件会明确阻止启动；新配置不要用于替换现有模型密钥。原README历史内容保持不变，使用及代码说明追加在`instructions.md`和本地`docs/rag-python-code-guide.md`。

验证：后端**366 passed、1 skipped**（可选PostgreSQL专项未运行），Ruff、前端typecheck、事件/拓扑/报告检查及生产构建通过；浏览器覆盖扩展开关、额外查询上限、原问题归一化与实际变体显示、不自动检索、参数恢复和既有空间管理/召回滑块，浅色、深色及移动端检查通过。所有验证仅使用本地测试Provider或拦截API，没有调用真实模型、重建知识空间或修改生产数据库。新测试临时目录显式指定在E盘的项目`build/`下；wheel打包已核验包含内置47条术语表。

## v0.7.0

发布日期：2026-10-15。**当前实现版本为0.7.0**，本节接续并取代前文0.6.0的当前状态说明；既有README内容按要求保留，不重写历史。

### 评测闭环

新增独立RAG/Agent评测模块、CLI、API及`/knowledge/evaluations`页面。40个RAG题覆盖现有LLM语料，34题核验实际原文hash、页码/标题和anchor，6个无答案题保留draft；10个Agent题覆盖审批、补充、重规划和恢复等契约。题集不包含编造的排名或Run：默认离线回放真实冻结输入，缺输入明确阻止执行。规则输出SourceRecall@5、MRR、child命中和context命中率；答案正确性与引用支持度没有人工标注/独立Judge时保持未评测，不冒充已通过。

真实执行先预检固定知识快照、配置和费用边界，再明确确认。默认前10题×2配置、并发2、单题90秒、总时限1200秒、最多60次业务HTTP尝试；模型重试、高德内部查询、天气两次请求及网页重定向共同记账。预算持久化后才允许发送，超限/超时/取消/重启保留partial，不自动增加额度续费。Provider内部不可见的搜索次数、数据库及LangSmith遥测不计入这项HTTP上限；费用usage缺失显示unknown，HTTP次数不等于严格美元封顶。

本地产物存于E盘`data/evaluations`，与生产Run库分离；可登记实际SearchResult或Run、逐题回放、比较和JSON/CSV导出。比较默认要求相同索引；整空间索引迭代显式使用index范围，题集、原文和gold仍须一致。独立Judge默认关闭，模型单独配置；LangSmith同步需实验显式选择，只上传指标和元数据。Agent live使用独立内存Run和冻结工具配置，不修改生产空间或Run记录；审批题驱动真实interrupt/resume，进程恢复题须导入真实重启Run后回放。

### 索引身份与固定快照

新索引保存不可变`EmbeddingSpec/IndexBuildSpec`，包含可信服务标识、请求模型、输入模式、预处理、观察维度、解析/切块实现指纹、child/parent尺寸与依赖。入队、执行、向量返回、原子提交及整空间发布均校验身份，阻止同维不同模型混写。查询按固定世代的规范选择已配置的可信Provider；不会从数据库读取任意服务URL，也不会把当前.env补成历史模型事实。

每次检索固定世代和文档版本清单，parent与child使用相同绑定；Run绑定自足知识快照，不跟随之后的发布、回退或快照逻辑删除。实验台可以只读检索历史空间版本，知识库显示索引身份、模型与维度。整空间暂存、验收、原子发布和历史回退继续保留，不改为逐文档版本迭代。

旧索引缺规范时显示`legacy_unknown`，原向量保留；混合检索降级关键词，纯向量明确拒绝，不能继续暗中使用当前Embedding比较未知旧向量。首次核验身份需要用户显式重建整个空间并发布，可能产生Embedding费用。本轮没有排队重建。遗留无绑定RUNNING摄取任务提供默认只读repair预览；确认旧worker停止后才能修复状态。前端新增独立queued/failed任务取消；running和正在构建的任务不会被直接取消。

### 执行持久化与手动恢复

应用改用官方PostgreSQLcheckpointer，新增安全阶段/节点/调用账本、持久化工具预算、幂等命令、outbox和删除墓碑。首版同一执行schema只允许一个应用进程。启动先验证执行与知识PG资源，再打开Run SQLite、对账状态；PG未就绪不会提前把Run标失败。WindowsSaver使用独立Selector线程适配，应用自身的事件循环不变。

进程中断的Run保留checkpoint、任务结果与已消耗工具额度，网页“继续执行”确认后沿用原Run ID；审批仍走审批面板，不自动通过。未知外部响应提示可能已产生费用，明确确认后才可继续；安全已确认结果可复用，高德临时路线内容不长期缓存。代码/配置/绑定不兼容时返回冲突并要求新建Run；旧Run没有checkpoint不能假装恢复。取消优先、并发审批/恢复和迟到outbox不会重复启动或复活删除记录。账本优先汇总跨重启调用、Token及费用，未知值不被局部Trace的零值覆盖；不承诺外部HTTP计费exactly-once。

### 前端、配置与数据库

遵循frontend-design延续现有中性/青绿工作台：评测参数、结果表、缺指标和降级直接可见，重用检索控制，不另造一套主题；恢复、真实执行、取消均有确认与防重复提交。保留向右增加关键词权重、向左增加语义权重，以及23类事件配色、浅深主题和移动布局。

`.env/.env.example`追加带中文注释的执行连接、评测存储/预算和独立Judge设置，没有覆盖已有API密钥。使用既有E盘langchain环境，新增官方checkpoint/psycopg依赖。全套带注释指令见[instructions.md](instructions.md)第15节；本地代码说明为`docs/v0.7.0-runtime-and-evaluation.md`，docs与评测产物仍被Git忽略。

已在**原PostgreSQL数据库**执行`0004_execution`及幂等Saver初始化；新增`atlasflow_execution`schema中的4张执行业务表和4张官方Saver表，不是新建数据库。VSCode中请刷新并展开该schema。初始化前后核验：19份文档、91个文档版本、6876个chunk、5个世代、91个job、3个构建、5个空间快照和2个空间不变，完整空间payload及活动指针也不变；SQLite的25条Run、25条投影、2236条事件不变。没有启动生产Run对账、导入worker或收费评测。

后续其他环境首次部署：停止旧API/worker，安装依赖，执行`python -m alembic upgrade head`与`python scripts/init_execution_store.py`，再按指令启动单进程API和前端。本机数据库初始化已完成，命令可重复运行。空间实际重建和评测真实采集是后续显式操作，不与代码部署捆绑。

### 验收与限制

2026-10-14全量后端显式启用真实PostgreSQL：**433 passed**，仅第三方Starlette/AnyIO弃用警告1条；包含真正子进程kill/restart，Planner及已完成任务未重跑，剩余任务继续执行。Ruff、前端typecheck、23类事件/拓扑/报告检查及生产构建通过；浏览器覆盖新评测与恢复、既有空间管理、浅色深色及手机界面，API拦截不触发真实模型。wheel核验包含40/10题集与内置术语表，构建和测试保留产物位于E盘。

2026-10-15收尾复验：Ruff、前端typecheck及事件/拓扑/报告检查通过；后端不启用PG专项时为**428 passed、5 skipped**。显式PG复验的3个需连接测试因本机Docker引擎未运行而连接拒绝，未通过的本次尝试不计入成功记录；另2个PG依赖的启动失败保护检查通过。上轮433项结果仅指2026-10-14数据库可用时的验证。启动Docker Desktop及原PostgreSQL容器后再做全量PG复验；不需要再次生成向量或修改知识版本。零调用全矩阵预检返回40题×6配置共240个缺失冻结输入，符合尚未采集真实排名的状态；Judge仍关闭。

2026-10-15收尾复验：Ruff、前端typecheck及事件/拓扑/报告检查通过；后端不启用PG专项时为**428 passed、5 skipped**。显式PG复验的3个需连接测试因本机Docker引擎未运行而连接拒绝，未通过的本次尝试不计入成功记录；另2个PG依赖的启动失败保护检查通过。上轮433项结果仅指2026-10-14数据库可用时的验证。启动Docker Desktop及原PostgreSQL容器后再做全量PG复验；不需要再次生成向量或修改知识版本。零调用全矩阵预检返回40题×6配置共240个缺失冻结输入，符合尚未采集真实排名的状态；Judge仍关闭。

当前完成的是工程可核验闭环，**尚未建立真实检索效果基线**，不能将测试通过称为Recall或报告质量已提升。本轮未调用真实Provider/Judge，也未将评测内容上传LangSmith；真实效果需用户显式采集后比较。本轮不包含LLMWiki、多轮会话、Studio同步、新工具或数据库侧TopK优化，没有提交或推送Git。

### v0.7.0评测领域分离重设计记录（2026-10-15，待实施）

用户实际运行后指出RAG与Agent不应共用一套实验。只读审查确认：Agent套用RAG对照配置/指标；现有Agent规则主要验证执行结束而非任务成功；Judge失败处理还可能污染已完成的检索指标。最近一次RAG实验30条全部因旧索引身份未知降级关键词，来源召回均值1.0掩盖了证据锚点0.5/0.9/0.9的差异；最近一次Agent实验约90秒因单题超时停止整批，4条均未完成，而HTTP额度仅用32/60。这些是用户运行后的观察，不是本次新增收费采集。

已参考[BEIR](https://github.com/beir-cellar/beir)、[Ragas](https://github.com/vibrantlabsai/ragas)、[AgentEvals](https://github.com/langchain-ai/agentevals)及Sierra任务基准，完成本地`docs/evaluation-redesign-v2.md`设计稿。拟采用独立RAG/Agent工作区，分离检索、RAG回答、Agent场景协议，仅共享安全基础设施；增加gold/实际证据对照、结构化轨迹断言、任务rubric、独立超时、显式缺测/降级和版本化比较。不以更改评分制造质量提升，不覆盖现有实验。本次只完成调研和设计，业务代码与前端尚未按v2改造，版本仍为0.7.0；真实重建和评测仍须显式确认。

### v0.7.0评测v2实施与对照实验补记（2026-10-15）

经确认实施上方设计。新增独立`/evaluations/rag`与`/evaluations/agent`工作区及分领域API；前者分纯检索、单次RAG回答协议，后者分任务场景、工程契约，不共享调参面板或质量总分。原`/knowledge/evaluations`改为只读历史入口，旧API保留兼容，既有实验与冻结输入不改写。v2是评测协议版本，应用仍为0.7.0。

RAG按真实child前K排名，分开文献覆盖、child锚点、parent上下文和回答语义项；采集深度不足不伪评更大的K，未审核qrels不计算nDCG。回答协议实际调用隔离生成器，保存真正发送的上下文，不用SearchResult冒充答案。Judge独立后处理，失败不清空检索/轨迹分；回答失败前的检索产物已独立冻结。单题超时不再主动停止整批，HTTP与全局时限、取消仍可停止实验。

结果主视图改为基线与候选的同题配对对照，展示两组数值、差值、可评分数量、改善/持平/退化及逐题依据；耗时按越低越好解释，缺测/失败/降级不混入正常效果均值。调参说明直接可见，避免把不同题目集合的均值差当作改善，亦不宣称小样本差异具有统计显著性。

Agent首轮聚焦工具差异：展示来自注册目录的工具、能力、角色权限及本地配置状态，两组共享模型和编排策略，仅白名单可变；后端再次约束并核对实际可用集合，避免名义不同但实际相同的伪对照。每组使用隔离Registry，禁用工具不注册、不进入Function Calling、不自动补回；不可用能力作为明确缺口保留。A05/A06/A08可用于知识检索与联网扩展比较；题集尚无地图/天气/计算专用任务，不据此宣称这些工具有效。流程完成与任务目标满足始终分开。

新增`evaluation/common`、`rag`、`agent`与`legacy`领域模块、实际执行绑定和`scripts/evaluate_v2.py`；提供RAG单变量重排、Agent工具组合的默认回放计划模板。原E盘独立评测SQLite新增`evaluation_v2_experiments`、`evaluation_v2_inputs`两张表，首次后端重启幂等初始化，不新增PostgreSQL迁移、不改变原文或索引。完整输入/理由/报告留在E盘，显式LangSmith同步仅上传元数据和指标。

使用指令追加在[instructions.md](instructions.md)第16节；本地详细说明为`docs/evaluation-v2-implementation.md`，继续不进入Git。本轮不自动采集收费基线、调用Judge、重建真实语料或提交Git；缺少参考事实与Agent已审核语义rubric的指标保持未评测，工程测试通过不等于真实检索或报告质量已提升。

本轮验收：后端**523 passed、5 skipped**（可选PostgreSQL专项未启用），仅第三方Starlette/AnyIO弃用警告1条；Ruff通过。前端typecheck、事件/拓扑/报告检查、生产构建和新`test:evaluations:v2:browser`通过；覆盖组间配对、0与null、降级排除、耗时/费用单位、neutral指标、工具目录及共享策略、确认防重复、浅深主题和移动布局。浏览器使用拦截API，不是真实质量实验，产物位于E盘`build/evaluation-v2-browser/check-S9g1AI`。wheel已核验新旧40/10/10题集、新评测模块和中英术语表。旧可写评测页的浏览器脚本保留历史，不冒充本轮只读页面回归。

### v0.7.0评测指标图形对照补记（2026-10-15）

按交互反馈，将同实验对照的主视图进一步改为可点击指标、切换候选组的图形分析。每个指标显示基线/候选配对均值横向柱图和逐题差值图，附真实样本覆盖、变化方向、指标含义及结果解读；表格保留用于核对。多候选组分别使用自己的相同配对子集，不借用另一个候选组的基线均值。

均值轴从0开始，逐题差值以0为中线；0值有明确标记，缺评与降级不伪装成0分。耗时按越低越好解释，中性调用数量仅记录变化，颜色与文字共同标识改善/持平/退化。比例和评分保留原值绝对差，不冒充相对提升；极小的费用/耗时保留有效数值。自动解读来自已保存的配对统计，不新增LLM调用，也不宣称统计显著性。

沿用frontend-design的既有中性/青绿、Geist与系统中文字体，以指标选择和两类对照图组织层级；支持键盘、浅深主题与移动端。新增纯图表模型`frontend/lib/evaluation-chart.ts`及离线命令`npm run test:evaluations:chart`，延续现有v2 API，不改数据库、评分器、预算或历史实验。使用方式与验收指令追加在`instructions.md`第17节。

图形增量验收：图表离线检查、typecheck、现有flow/ui/report回归和生产构建通过；扩展浏览器回归包含97次拦截夹具请求，运行异常0，覆盖点击与键盘、候选切换、0/缺评、实际USD单位、低值优先与中性方向，以及桌面/手机/深色图表。最终截图及诊断位于E盘`build/evaluation-v2-browser/check-eWQCfh`；独立测试服务已停止，验收构建自动修改的类型配置已恢复。没有真实Provider请求或新增费用。

### v0.7.0多组同图与逐题详情弹窗补记（2026-10-16）

按本轮反馈移除均值图中的基线/候选选择，所有实验配置组同时绘制在同一值轴上，使用稳定组色与原始配置名称，不赋予角色优先级。点击指标后同步更新各组均值、逐题表现与解释。各组均值使用所有组同题、同次重复的共同可评交集，失败、降级、缺评、非有限值及评分契约不一致均排除，避免拼接不同样本分母；共同覆盖与各组自身可评覆盖分开说明。逐题表现也使用所有组同一值轴，不再以某组为基准减分。

删除页面的「核对数据」模块，不用折叠或隐藏替代。逐题记录保留，点击「查看详情」以大尺寸原生dialog展示评分、证据、报告和诊断；支持关闭按钮、Esc、遮罩、焦点约束与返回、背景滚动锁定。关闭会使正在读取的旧详情失效，迟到响应不会重开弹窗；读取失败或没有冻结输入时，已保存评分和关闭操作仍可使用。

按追加要求移除「历史实验」导航与页面展示，旧`/knowledge/evaluations`地址跳转至`/evaluations/rag`。只移除界面，不清空原评测SQLite、不删除v1记录、冻结输入或原始数据；兼容API暂保留。现有v2「实验记录」仍用于选择本次各领域实验。应用版本仍为0.7.0，README只追加，操作说明接续在`instructions.md`第18节。

本轮前端验收：图表确定性检查、typecheck、flow/ui/report回归与生产构建通过。浏览器98次拦截夹具请求、0运行异常，验证三组同轴和共同样本均值、0/缺评、核对模块移除、原生弹窗的Esc/遮罩/Tab焦点与背景锁定、关闭回焦、迟到响应丢弃、读取失败/无输入时保留评分、历史入口移除和旧地址跳转，以及桌面/手机/深色视图。修复了StrictMode重开dialog后旧close事件误关闭新弹窗的问题。截图与诊断位于E盘`build/evaluation-v2-browser/check-eU3ajY`；测试服务已停止，构建自动类型路径恢复默认。本轮没有真实模型调用、数据库数据删除或Git推送。


### v0.7.0RAG真实执行与实验删除接口补记（2026-10-16）

RAG实验页不再提供冻结回放、冻结输入匹配或输入选择；每次按当前参数重新检索，“检索与回答”还会重新生成答案。v2 RAG契约只接受`mode=live`，非空`input_refs`和`mode=replay`均拒绝，RAG的`/inputs`导入与列表入口移除。预检允许`confirm_live=false`且不调用模型，创建实验仍要求`confirm_live=true`，页面仍有预算确认与独立Judge确认。示例计划已改为live，但保留未确认状态，避免直接执行产生费用。Agent实验的回放能力不变；已保存的旧实验和证据仍可读取，不做数据清理。

新增`DELETE /api/v1/evaluations/rag/experiments/{experiment_id}`与`DELETE /api/v1/evaluations/agent/experiments/{experiment_id}`。默认软删除：返回删除时间与`artifacts_retained=true`，之后列表、详情、导出、产物读取和对比不再返回该实验。不存在或跨领域访问返回404，运行中或尚未完成收尾返回409；同领域重复删除幂等返回原删除结果。保留原始数据、共享证据和其他实验，禁止迟到写入复活已删除记录；不删除LangSmith上的追踪数据。当前只增加API与客户端方法、CLI，不添加页面删除按钮，也没有替用户删除任何现有记录。

删除标记保存在现有评测SQLite表`evaluation_v2_experiments`的JSON payload内，不新建数据库、不修改知识库PostgreSQL、不需要Alembic迁移。命令新增`evaluate_v2.py delete --kind ... --experiment ... --confirm-delete`，完整注释示例追加在`instructions.md`第19节。界面沿用frontend-design的既有布局，只减少不再需要的RAG控件；本项目版本保持0.7.0。

本轮验收：后端全量536通过、5跳过；前端typecheck、图表/flow/ui/report与生产构建通过。浏览器94次拦截夹具请求、0异常，截图与诊断在`build/evaluation-v2-browser/check-4IlQNj`。未调用真实模型，未删除现有实验；3216专用测试服务已停止，Next自动改写的类型路径恢复默认。仅Starlette既有弃用警告，未执行Git提交或推送。


### v0.7.0实验记录删除交互与配对模块移除补记（2026-10-16）

RAG和Agent的「实验记录」每行新增独立的「删除」操作，接入上一轮的DELETE接口。记录选择与删除是并列按钮，不嵌套，不以悬停菜单隐藏。确认弹窗以危险操作样式显示准确实验ID、删除范围及证据保留说明；默认聚焦「返回」，支持Esc取消。运行中记录禁止删除，须先停止并等待结束；请求期间禁用重复操作，成功后移除行并提示，失败/409则保留记录、选中结果与错误原因。

删除当前选中记录时清空结果和详情；删除其他记录不改变当前查看内容。删除标记同时阻止迟到的列表、详情选择和轮询响应重新展示已删除实验；删除后焦点回到记录标题，最后一条删除后展示空态。没有自动切换到其他实验，也没有自动取消、重跑或删除原始知识数据。

本轮验收：前端typecheck、chart/flow/ui/report回归与生产构建通过；删除API、CLI与RAG集成17项测试通过。浏览器145次拦截夹具请求、10次夹具DELETE、0运行异常，覆盖取消与失败、当前/非当前删除、迟到响应、刷新、空态和多尺寸主题。截图与诊断位于`build/evaluation-v2-browser/check-6ihV8R`。没有真实模型调用或现有实验删除，3216专用服务已停止，构建生成的类型路径已恢复默认。

移除两个页面底部的独立「配对比较」模块、基线/候选实验选择、对应组件与专用样式；保留单次实验内的「所有配置组效果比较」、逐题图、详情弹窗与下载。已有后端比较API仍兼容保留，不在界面发起请求。沿用frontend-design的中性/青绿、Geist与系统中文字体，删除操作只以克制的危险色区分，支持移动端和深色主题。界面说明追加在`instructions.md`第20节；版本仍为0.7.0。


### v0.7.0主模型复用与实验模型下拉补记（2026-10-16）

当前Agent、RAG回答和评审统一以`LLM_MODEL`为默认模型。Judge的`EVALUATION_JUDGE_MODEL`留空时回退到主模型，Key/BaseURL留空继续复用主连接；显式评审覆盖仍优先。v1/v2的实际请求、来源上下文、评审身份与grader_id统一记录解析后的模型名，避免“调用主模型但元数据为空”。保留用户已有Judge开关；只有实验勾选并确认独立评审后才额外调用，不自动运行付费实验。Embedding和Rerank仍使用专用模型，不改索引或向量。

RAG「回答模型」与Agent「共同执行模型」由文本框改为原生下拉框，默认项直接显示主模型名。候选来自后端settings的`models/default_model`，不在前端硬编码，也不自动拉取第三方目录。新增`LLM_MODEL_OPTIONS`配置额外聊天模型ID的JSON数组；主模型始终包含，去空格与去重，禁止URL/非法候选。当前本地保持空数组，因此只提供`~deepseek/deepseek-flash-latest`；新增候选统一使用现有LLM服务连接，名单不代表远端可用性已验证。

Judge区域显示实际评审模型及是否复用主模型；目前Judge模型固定于服务端，不随前端选择其他回答模型而悄悄改变。保留两组共同模型约束，模型切换会使旧预检失效。支持配置中的`~`前缀模型别名，但仍拒绝把URL当作模型ID。沿用frontend-design的现有字段、字体和布局，不增加自由输入或隐藏菜单。使用方式见`instructions.md`第21节，项目仍为0.7.0。

本轮验收：后端全量547通过、5跳过，前端typecheck、chart/flow/ui/report和生产构建通过。生产浏览器147次拦截夹具请求、0异常，验证下拉选模与传参、别名、预检失效、字段对齐、评审模型展示，以及删除/图表/多尺寸主题回归。截图与诊断位于`build/evaluation-v2-browser/check-9dIKem`；仅使用测试Provider与一次性夹具，未发起真实模型请求或删除既有记录。专用3216服务停止，Next类型配置恢复默认，未提交或推送Git。

同模型生成与评审是独立请求/提示词，不代表统计独立或客观真值，存在同源评审偏差；后续可单独配置Judge模型。没有新增数据库迁移或知识空间重建，也没有修改任何密钥。

### v0.7.0实验选项精简与只读诊断补记（2026-10-16）

RAG与Agent实验页移除「同步LangSmith摘要」控件，RAG页移除「允许降级诊断」控件，同时删除相应状态、组件参数和专属样式，不通过CSS隐藏替代。网页提交的`langsmith_sync`与`allow_degraded`固定为false，延续原有未勾选默认，不静默开启同步或放行降级；保留后端API能力、索引身份与语义服务校验、既有全局LangSmith追踪、Judge确认以及历史降级结果的诊断展示。沿用frontend-design的现有布局，减少无关控件和空容器。

本轮验收：typecheck、chart/flow/ui/report与生产构建通过，生产浏览器147次拦截夹具请求、0异常，验证两个控件不存在、预检和创建请求的两个字段保持false，并回归模型选择、删除确认、详情、图表及响应式视图。最终截图和诊断位于`build/evaluation-v2-browser/check-L5sJU6`。测试服务停止，Next生成类型路径恢复默认，实验数据库修改时间未变化，没有真实Provider调用或Git提交/推送。

按要求只读检查最新实际实验记录，将生成失败、Judge失败、HTTP成功与标注缺失分开解释。没有替用户修改模型/Token预算或自动重跑，没有写入、删除实验数据或发起付费模型请求。完整逐项诊断保存在被Git忽略的`docs/latest-rag-experiment-diagnosis-2026-10-16.md`，界面说明追加在`instructions.md`第22节；版本仍为0.7.0。

### v0.7.0实验检索参数去重补记（2026-10-16）

实验检索参数区不再显示「启用重排」「中英查询扩展」两项只读复选框及「由选中的对比配置决定」提示，直接按fixedFeatures条件不渲染，而非CSS隐藏。各对照组的rerank_enabled/query_expansion_enabled仍由配置定义；没有关闭实际重排或扩展。重排候选、融合权重、RRF及扩展查询上限继续可按原规则调节；知识库单次检索页的两个可编辑开关保留。

本轮验收：控件离线渲染、图表检查、typecheck与生产构建通过；生产浏览器147次拦截夹具请求、0异常，验证重复项移除、实际参数与扩展上限保留，并回归现有模型、删除、图表和多尺寸主题。截图与诊断位于`build/evaluation-v2-browser/check-z9T8xM`，已检查桌面布局；专用测试服务已停，生成类型路径恢复默认。无真实模型请求、数据库修改或Git提交/推送。

沿用frontend-design现有布局精简重复信息，不更改模型、Token预算、RAG后端、实验记录或索引。新增`npm run test:retrieval:controls`离线组件渲染检查，覆盖固定/可调模式、四种功能组合、数值参数保留；浏览器增加无重复开关和对照组实际参数未变的断言。操作追加在`instructions.md`第23节，版本仍为0.7.0。

### v0.7.0回答预算、Judge输出与参考答案补记（2026-10-16）

RAG回答生成器默认输出上限由1600提高至4096Token，提示词更新为rag-answer-v2.1：提高可用输出预算，同时要求聚焦问题，不扩写长综述。前端默认值从服务端设置读取，仍可调低；各对照组共享相同生成器。原rag-answer-v2提示词继续接受，不静默改写显式指定的旧配置。此处是隔离回答实验，不改变工作流合稿Agent的详细报告策略。

Judge不再固定2200Token，新增EVALUATION_JUDGE_MAX_TOKENS（默认4096）及EVALUATION_JUDGE_MAX_REASON_CHARS（默认160）；.env与.env.example均附注释。评分提示词更新为evidence-rubric-judge-v2.1，明确列出各评分项，一项一句依据；超长理由限长并记录诊断。预算、理由上限、模型和提示词版本进入Judge身份。截断仍记失败，不把不完整答案/JSON当成完成，不增加自动重试或全局HTTP/时间预算。

为最新实际实验涉及的R01、R04新增独立reference-answers-v2.json标注，核对本地Transformer与Chinchilla原论文的内容哈希、页码及原文锚点，保存参考答案与逐条事实。原v1题集不变；v2题集版本为2.1，其他38题继续缺少参考答案，不用检索锚点或模型常识伪造gold。参考答案只用于展示/评审，不传给回答生成器；不构成完整的40题答案基准或人工审批。

数据集接口新增dataset_hash；详情只在题集ID及完整标签哈希都匹配时显示当前标注，避免用新参考答案解释旧实验评分。历史结果、证据和分数不重写，标签变化后的实验不冒充同一基准。沿用frontend-design现有表单和详情结构，只补预算说明、参考字段及版本不匹配提示。操作见instructions.md第24节；需要重启后端、刷新前端，无数据库迁移、索引重建或付费自动重跑，项目仍为0.7.0。

本轮验收：后端565通过、5跳过，仅Starlette既有弃用警告；前端typecheck、检索控件、chart/flow/ui/report和生产构建通过。生产浏览器154次拦截夹具请求、0异常，覆盖预算越界阻止、默认值与提示词提交、Judge限制展示、当前参考答案和历史标签隔离，并回归删除、图表、弹窗和多尺寸主题。截图与诊断位于build/evaluation-v2-browser/check-oXGtws，已目视检查；wheel包含新参考标注且无源码回退加载通过。专用测试服务已停止，Next类型路径恢复默认；真实实验库大小/修改时间不变，无真实模型调用、Git提交或推送。

### v0.7.0评测主指标收敛与多组数值对照补记（2026-10-16）

按协议固定主指标：RAG检索5项、RAG检索与回答8项、Agent任务8项、Agent工程契约7项。清单集中在frontend/lib/evaluation-metrics.ts，不按本次分差动态挑指标，也不把其他诊断项补入空位。保留来源覆盖、引用合法性等原始诊断与JSON/CSV导出，不重写历史评分；详情评分区同步收敛为主指标，仍可查看原始产物、失败原因与执行断言。

RAG主视图聚焦child证据覆盖@5、首位命中@1、MRR@5和耗时，纯检索另看parent上下文覆盖；回答实验另看参考事实覆盖、正确性、事实忠实度和引用支持。新网页实验固定按@1与@5计算，返回片段至少5条；API对其他K值的兼容不变。文献覆盖、返回条数与引用ID合法性不再占主指标槽位，缺少完整qrels的nDCG也不作为主指标。不把正例锚点覆盖改称标准全量Recall或Context Precision。

以主指标数值对照替换无数值的指标按钮和重复下拉框：每行同时列出所有配置均值、共同可评数量与最大最小差，点击指标查看原有均值柱图和逐题图。表格与图表共享buildMultiGroupChart的同题/同次重复全组交集；不使用各组不同样本的独立均值拼接。比例显示百分比，比例绝对差显示个百分点；MRR保留0—1排序分，耗时、费用保留原单位。持平、缺评、只有单组、历史未采集均明确提示，不放大坐标或编造0分。窄屏横向滚动，指标名称固定，保留键盘焦点与深色主题；沿用frontend-design现有配色、字体与布局。

选型参考[BEIR的检索排序指标](https://github.com/beir-cellar/beir)、[Ragas的分维度指标](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/)及[τ-bench的任务/工具/策略划分](https://github.com/sierra-research/tau2-bench)。仅借鉴指标选择与实验边界，不引入新依赖、额外Judge请求或未经标注的数据。最新实际RAG实验只有2题，部分答案质量项已饱和；更直观展示不代表新评分、更好质量或统计显著性。操作见instructions.md第25节，详细说明在本地docs/evaluation-metric-selection.md；项目仍为0.7.0。

本轮验收：相关后端51项通过；前端typecheck、chart、检索控件、flow/ui/report及最终生产构建通过。最终浏览器165次拦截夹具请求、0异常，覆盖5/8/8/7指标、六组值与图表一致、百分比/百分点、缺评不补0、历史缺项、键盘、手机固定名称列、深色主题及既有删除/模型/弹窗回归。截图已目视检查，位于build/evaluation-v2-browser/check-a5E5en；合成夹具不是真实质量测量。专用3216服务停止，Next类型配置恢复；不修改真实实验数据、不调用真实模型、不提交或推送Git。

