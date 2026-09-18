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
