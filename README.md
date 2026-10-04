# AtlasFlow

当前版本：`v5.0.0`。最新变更见文末的 [v5.0.0](#v500--原生工具调用与地图导航2026-09-26)；
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
