## 整个项目执行指令（VS Code PowerShell）

以下命令以 `E:\codex\agent` 为项目根目录、`E:\conda_envs\langchain` 为既有 Python 环境。首次准备、后端、前端和 Studio 建议使用不同终端；**每开一个终端，先执行公共环境设置**。命令旁的注释说明用途；标有“外部请求”的步骤可能使用模型／检索服务配额。所有项目目录、缓存和临时文件都指定到 E 盘。不要在终端打印 `.env` 中的 Key。

### 1. 每个新终端：公共环境设置

```powershell
# 进入项目根目录；后面的相对路径均以这里为起点。
Set-Location E:\codex\agent

# Python、pip、Studio 和其他命令的临时文件与缓存保持在 E 盘。
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:PIP_CACHE_DIR = 'E:\codex\cache\pip'
$env:UV_CACHE_DIR = 'E:\codex\agent\.uv-cache'
$env:PYTHONPYCACHEPREFIX = 'E:\codex\agent\.pycache'
$env:PYTHONPATH = 'E:\codex\agent\backend\src'
$env:NPM_CONFIG_CACHE = 'E:\codex\agent\.npm-cache'
$env:DOCKER_CONFIG = 'E:\codex\agent\.docker'
$env:ATLASFLOW_POSTGRES_DATA = 'E:/codex/agent/data/postgres'

# 只读检查：应返回 True；如果 Python 路径不存在，先确认既有 conda 环境位置。
Test-Path -LiteralPath 'E:\conda_envs\langchain\python.exe'
```

Docker Desktop 本身要先启动，且需确认其 WSL 镜像、缓存也配置在 E 盘；`ATLASFLOW_POSTGRES_DATA` 只约束此项目的 PostgreSQL 数据卷，并不能迁移 Docker Desktop 自身的数据。使用浏览器下载报告时，也请把下载目录设在 E 盘。

### 2. 首次安装或依赖变化时执行

```powershell
# 仅当 .env 不存在时从模板创建；已有 Key 的 .env 不会被覆盖。
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }

# 按 .env.example 的说明手动填写本机 .env；不要提交、复制到聊天或输出密钥。
# 首次安装 Python 项目、测试依赖和 Studio CLI；会访问包索引并修改 E 盘环境。
E:\conda_envs\langchain\python.exe -m pip install --no-user --cache-dir E:\codex\cache\pip -e '.[dev,studio]'

# 首次安装前端依赖；npm ci 会重建本项目 E 盘 frontend/node_modules。
Set-Location E:\codex\agent\frontend
npm ci
Set-Location E:\codex\agent
```

后端至少要在 `.env` 配好 `LLM_*`、`EMBEDDING_*`、`RERANK_*` 和 `KNOWLEDGE_DATABASE_URL` 才能执行真实任务；LangSmith 和地图 Key 按需配置。`LANGSMITH_TRACE_CONTENT=true` 会把查询、证据和报告正文上传到 LangSmith，处理私有语料时应谨慎。安装依赖不会自动下载论文语料。

### 3. 日常启动：数据库 → 后端 → 前端

在终端 A 执行公共环境设置，然后运行：

```powershell
# 启动 pgvector PostgreSQL；首次运行会下载镜像并在 E 盘创建数据库文件。
docker-compose up -d postgres

# 只读查看容器状态；应看到 postgres 正常运行。
docker-compose ps

# 将数据库结构升级到当前 Alembic 版本；可能写入数据库结构，不导入语料。
E:\conda_envs\langchain\python.exe -m alembic upgrade head

# 启动 FastAPI（占用当前终端，Ctrl+C 停止）；默认 8000 端口。
# .env 中 KNOWLEDGE_WORKER_ENABLED=true 时，内置 Worker 处理已排队的导入／重建 Job。
E:\conda_envs\langchain\python.exe -m uvicorn atlasflow.main:create_app --factory --app-dir backend/src --reload --port 8000
```

在终端 B 执行公共环境设置，再运行：

```powershell
# 只读检查后端健康状态；先确认这个请求成功，再启动前端。
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/health'

# 启动 Next.js 开发前端（占用当前终端，Ctrl+C 停止）；默认 3000 端口。
Set-Location E:\codex\agent\frontend
npm run dev
```

前端地址为 `http://localhost:3000`，后端 API 文档为 `http://127.0.0.1:8000/docs`。网页新建 Run 时可选择“不使用知识库”或指定知识空间；开始真实 Agent Run 会调用 OpenRouter 等外部服务，可能产生费用。启动后端本身不会自动下载清单语料。

### 4. 可选：LangSmith 与 LangGraph Studio

在 `.env` 中填写 `LANGSMITH_API_KEY` 并设 `LANGSMITH_TRACING=true` 后，**重启后端并新建 Run**，旧 Trace 不会补录。另开终端执行公共环境设置，再用下列命令只检查状态／连通性：

```powershell
# 只读查看脱敏后的本机配置状态；不会显示 API Key。
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/settings/status'

# 主动向 LangSmith 检查连接；会产生一次外部网络请求，不创建 Agent Run。
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/api/v1/settings/langsmith/check'
```

Studio 需要单独终端及首次安装时的 `.[studio]` 依赖：

```powershell
# 从项目根目录启动本机 LangGraph Agent Server；脚本固定端口 2024 和 E 盘缓存。
.\scripts\start-studio.ps1
```

在另一个已执行公共环境设置的终端检查：

```powershell
# 只读检查 Studio 服务是否就绪。
Invoke-RestMethod 'http://127.0.0.1:2024/ok'
```

Studio 页面为 `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024`。选择 `atlasflow` 图后再运行任务，例如在输入面板提交以下 JSON（这是 Studio 输入，不是 PowerShell 命令）：

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

**Studio 的测试 Run 与网页后端的 RunRecord 独立，不会自动同步到网页运行记录**。在 Studio 执行真实图同样会调用模型／工具并可能产生费用。

### 5. 可选：从终端创建或查看 Agent Run

网页是主要入口；下面是与网页等价的 API 示例。在已运行后端的新终端执行公共环境设置，再运行：

```powershell
# 创建一个不使用知识库的研究 Run；会触发模型和工具调用，可能产生费用。
$runBody = @{ query = '介绍黄山风景'; auto_approve = $true; policy = @{ knowledge_mode = 'off' } } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/runs' -ContentType 'application/json; charset=utf-8' -Body $runBody

# 只读查看 Run 列表；根据返回的 id 到网页详情或 API 查询具体运行。
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/runs'
```

如果这个 Run 要检索指定空间，把 `policy` 改成 `@{ knowledge_mode = 'selected'; knowledge_space = 'llm-engineering-v1' }`。运行结果不保证自动使用 RAG；是否需要 `knowledge_search` 仍由规划与工具决策确定。

### 6. 可选：语料清单、首次导入和现有索引重建

先确保数据库与后端已启动。以下命令均在项目根目录执行；下载／入库只在你显式运行时发生：

```powershell
# 只读验证语料清单；不下载论文或写入知识库。
E:\conda_envs\langchain\python.exe -m atlasflow.knowledge.cli validate corpus_manifests/llm-engineering-v1.yaml

# 只读列出知识空间及目标空间文档；用于确认所选空间。
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/knowledge/spaces'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/knowledge/spaces/llm-engineering-v1/documents'

# 仅首次需要原文时：从清单来源下载到 E 盘；有外部网络与磁盘写入。
E:\conda_envs\langchain\python.exe scripts/import_llm_corpus.py --download

# 仅首次需要建立索引时：解析本机原文、写 PostgreSQL 并调用 Embedding API。
E:\conda_envs\langchain\python.exe scripts/import_llm_corpus.py --ingest
```

**已有 18 篇原文和旧索引时，不需要再次下载或首次导入。**本次 v2 解析／切块升级用下面两条命令；先预览，再决定是否排队：

```powershell
# 只读预览旧版文档：只调用本地后端 GET，不创建 Job，不调用 Embedding。
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1

# 确认后显式排队：创建新版本与 Job；Worker 随后调用 Embedding API，可能产生费用。
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1 --enqueue

# 只读查看该空间所有 Job；关注 status、stage、error_code 和 error_message。
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/knowledge/jobs?space=llm-engineering-v1'
```

其他空间将 `--space` 和 URL 中的 `llm-engineering-v1` 换成实际空间名，分别执行。`--enqueue` **只排队**，须有后端内置 Worker 正在运行；若明确关闭了内置 Worker，可在另一个已设置 E 盘环境的终端执行 `E:\conda_envs\langchain\python.exe -m atlasflow.knowledge.worker`。不要同时把这条命令误当成“免费预览”。

查看单篇文档当前发布版本时，先从文档列表复制真实 ID，再运行：

```powershell
# 把此占位值换成文档列表中的真实 document.id；只读查看版本与发布指针。
$documentId = '<文档ID>'
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/knowledge/documents/$documentId"
```

验证当前检索效果时可请求调试搜索；该请求会调用 Query Embedding 和 Rerank，可能使用外部额度：

```powershell
# 查询指定空间，查看 hits、decision 和 trace；不会更改已发布索引。
$searchBody = @{ query = 'Transformer 的注意力机制是什么？'; space = 'llm-engineering-v1'; result_limit = 5 } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/knowledge/search/debug' -ContentType 'application/json; charset=utf-8' -Body $searchBody
```

### 7. 可选：离线测试与前端构建

测试不用真实 OpenRouter／高德 Key；先按第 1 节设置 E 盘环境。前端命令在 `frontend` 目录执行：

```powershell
# 后端单元／集成测试与静态检查；测试临时文件仍在 E 盘。
E:\conda_envs\langchain\python.exe -m pytest backend/tests -q
E:\conda_envs\langchain\python.exe -m ruff check backend scripts

# 前端类型检查和现有三类确定性 UI／流程／报告验证。
Set-Location E:\codex\agent\frontend
npm run typecheck
npm run test:flow
npm run test:ui
npm run test:report

# 可选生产构建；使用 E 盘隔离输出目录，避免与正在运行的 dev 服务器共用 .next。
$env:ATLASFLOW_NEXT_DIST_DIR = '.next-build'
npm run build

# 可选浏览器视觉回归：需要前端已在 3000 端口运行；隔离浏览器资料与产物写入 E 盘。
# 这里使用模拟报告数据，不创建真实 Agent Run。
npm run test:report:browser -- http://localhost:3000
```

只有需要真实供应商端到端联调时，才在项目根目录显式运行下列在线冒烟脚本；两者都会发起外部请求，第二条还会完整执行 Agent 工作流，不属于离线测试：

```powershell
# 分别检查真实 LLM、Embedding、Rerank、网页搜索配置；可能产生费用。
E:\conda_envs\langchain\python.exe scripts/live_provider_smoke.py

# 运行一整条真实 Agent 研究流程；可能调用模型、检索和工具并写入本地 Run。
E:\conda_envs\langchain\python.exe scripts/live_workflow_smoke.py
```

### 8. 停止服务

后端、前端和 Studio 各自在其终端按 `Ctrl+C`；这只停止进程，不删除 Run 或知识库。数据库可在项目根目录执行：

```powershell
# 停止 PostgreSQL 容器但保留 E 盘数据卷；下次用 up -d postgres 恢复。
docker-compose stop postgres
```

不要用 `docker-compose down -v` 清理数据卷；它会删除数据库数据。上述浏览器页面和下载行为也应遵守 E 盘存储策略。
