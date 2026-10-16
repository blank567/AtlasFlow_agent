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

### 9. 可选：保存与切换整个知识空间版本（v0.6.0）

空间版本是一次完整快照：记录该空间每份文档的发布版本、归档状态和索引世代。文档摄取或归档后，空间成为“未保存变更”；待所有摄取任务完成，可保存一个新空间版本并附加说明。切换旧版本时，空间内全部文档一起恢复；旧快照之后新增的文档会归档而非删除。以下命令中 `activate` 是真实写操作；只查看版本时不要执行它。

首次使用先在项目根目录迁移 PostgreSQL（不会删除现有文档）：

```powershell
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
E:\conda_envs\langchain\python.exe -m alembic upgrade head
```

```powershell
# 只读：查看空间状态及已保存的完整空间版本。按实际空间 slug 修改。
$space = 'llm-engineering-v1'
$spaces = Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/knowledge/spaces'
$spaces | Where-Object slug -eq $space | Select-Object slug, active_generation_id, active_version_id
$versions = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/knowledge/spaces/$space/versions"
$versions | Select-Object id, version_number, generation_id, note, created_at

# 写操作：所有摄取任务完成后，把当前整空间保存为一个新版本，不重算向量。
$body = @{ note = '统一解析和父子切块完成' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/knowledge/spaces/$space/versions" -ContentType 'application/json; charset=utf-8' -Body $body

# 可选：只修改某个空间版本的说明，不影响文档或检索。
$versionId = '<空间版本ID>'
$body = @{ note = 'v1：重建前的完整空间快照' } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:8000/api/v1/knowledge/spaces/$space/versions/$versionId/note" -ContentType 'application/json; charset=utf-8' -Body $body

# 真实切换：确认目标版本的文档清单后再执行；整个空间的检索结果会改变。
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/knowledge/spaces/$space/versions/$versionId/activate"
```

页面中对应入口为知识库首页的「空间版本」。创建或切换需要空间无运行／排队摄取任务；索引缺失、历史文档已被永久删除等情况返回 409。接口不调用 Embedding，也不删除其他空间版本。旧空间版本只有在之前显式保存过快照时才能切回；现有数据库不会自动补造历史快照。

### 10. 从已有文档版本回填空间版本（一次性操作）

2026-10-11 已对当前数据库执行回填：`llm-engineering-v1` 得到 v1/v2，`user-default` 得到 v1，不要再次对这两个空间运行 `--apply`。新环境若保留了完整旧文档版本但没有空间版本，可先只读预览：

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:PYTHONPATH = 'backend/src'
E:\conda_envs\langchain\python.exe scripts/backfill_space_versions.py --space llm-engineering-v1 --space user-default
```

预览核对文档数量、解析／切块版本和索引世代后，才在**尚无空间版本**的数据库中执行一次写入：

```powershell
E:\conda_envs\langchain\python.exe scripts/backfill_space_versions.py --space llm-engineering-v1 --space user-default --apply
```

脚本只使用现存文档、版本和 child 索引，拒绝未完成任务、缺失嵌入、混合索引世代、版本波次不齐或已有空间版本；不会调用模型或重建索引。回填记录的创建时间是执行时间，说明中另记原索引日期。

### 11. v0.6.0 整空间解析升级与双路召回

版本迭代的单位是整个知识空间。解析器版本为 `structured-parser-v3`，切块器仍为 `parent-child-v2`；PDF 新增章节路径和 `section_role`，其他格式也在同一批次重新索引。Top-K/RRF 检索代码更新后立即作用于现有索引；新解析结果需重建才可用。原空间版本 v1/v2 保留。以下命令在项目根目录、`langchain` 环境运行。

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:PYTHONPATH = 'backend/src'

# 更新数据库结构，包含 knowledge_space_builds；然后重启 API 和独立 Worker（若使用）。
E:\conda_envs\langchain\python.exe -m alembic upgrade head

# 只读：列出整个空间的全部 active 文档；archived 状态也保留在版本清单内。
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1

# 写操作：建立整空间构建，输出 id 即 build_id。Worker 会调用 Embedding API。
# 新索引暂存，所有文档仍使用旧空间版本对外检索。
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1 --enqueue --note '统一解析 v3，PDF 章节与参考文献角色'

# 将这里替换为上一步返回的真实 id；不是空间版本 id 或 Job id。
$spaceBuildId = '<build_id>'

# 只读验收：任务全部完成时 status=ready、退出码为 0；已发布也返回 0。
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1 --build $spaceBuildId --verify

# 写操作：全部完成后，校验完整索引并在一个事务中发布新的整个空间版本。
# 自动创建新空间版本快照，无需再手动“保存当前空间版本”。
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1 --build $spaceBuildId --publish

# 只读查看任务及空间版本台账。
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/knowledge/jobs?space=llm-engineering-v1'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/knowledge/spaces/llm-engineering-v1/versions'
```

也可在知识库页面的「空间版本」区域执行「重建整个空间 → 发布整个空间版本」。有失败任务时可以在构建进度中重试；失败不会改变旧版本检索。构建期间暂停该空间的导入、归档、保存快照、切换历史版本和另一轮重建，以固定文档清单；检索仍可使用。已归档文档不参与重建，但归档状态保留在完整版本清单中。

取消或重试是单独的选择，不要紧接上面的正常发布命令执行：

```powershell
# 写操作：重试指定失败 Job，不是 build_id。
$failedJobId = '<failed_job_id>'
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/v1/knowledge/jobs/$failedJobId/retry"

# 写操作：放弃待发布构建，保留旧版。必须等待当前 running Job 结束后才能取消。
# 暂存数据保留用于排查，取消后不能恢复该构建；可重新发起一轮整空间构建。
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1 --build $spaceBuildId --cancel
```

`--enqueue` 不会自动发布，`--verify` 不调用模型，`--publish` 不重算向量。历史空间切换仍使用第 9 节的 `/versions/{version_id}/activate` 接口。解析器版本、切块器版本和空间 v1/v2/v3 是不同编号：空间版本在成功发布时顺序分配。空间版本恢复文档清单和索引，不恢复旧应用程序或旧检索算法。

## 12. 前端视觉与交互验收（v0.6.0补记）

以下浏览器脚本使用拦截API的本地样例，不需要后端，不会触发模型费用或修改知识库。需要已安装的Chrome或Edge；浏览器配置、截图和下载产物全部写入E盘 `build/`。

```powershell
# 终端A：启动独立验收页面，不与常用的3000端口共用构建缓存。
Set-Location E:\codex\agent\frontend
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:NPM_CONFIG_CACHE = 'E:\codex\agent\.npm-cache'
$env:NEXT_TELEMETRY_DISABLED = '1'
$env:ATLASFLOW_NEXT_DIST_DIR = '.next-design-check'
npm run dev -- --port 3210
```

```powershell
# 终端B：静态类型及已有渲染安全、拓扑与事件映射检查。
Set-Location E:\codex\agent\frontend
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:NPM_CONFIG_CACHE = 'E:\codex\agent\.npm-cache'
npm run typecheck
npm run test:flow
npm run test:ui
npm run test:report

# 全页面：桌面/移动/深色截图、文字对比度、具名SSE事件、审批与诊断入口。
node scripts/verify-design-browser.mjs http://localhost:3210

# 首页输入与运行操作：长度限制、策略继承、预算校验、失败重试、确认及筛选。
node scripts/verify-run-ui-browser.mjs http://localhost:3210

# 知识库：整空间构建锁定/重试/发布/回退，检索参数、评分与原文交互。
node scripts/verify-knowledge-browser.mjs http://localhost:3210

# 报告：固定目录、真实Markdown/HTML下载、打印隔离与PDF生成。
node scripts/verify-report-browser.mjs http://localhost:3210
```

脚本输出各自的产物目录；API样例仅用于验收，不是应用的Mock运行模式。浏览器脚本不会清理自己的截图与配置，确认无需保留后可手动删除指定的 `build/*-browser/check-*` 目录。结束终端A后，后续正常启动前清除该终端的临时构建变量，或使用新的终端。

## 13. 历史空间版本删除（v0.6.0补记）

新增接口需要重启后端加载，无需新数据库迁移。推荐在知识库「历史版本与说明」中删除，并核对确认框里的版本号。当前使用的版本不能删除；构建或摄取期间禁止新的删除。修改解析/切块/Embedding后，应按第11节重建并发布，不再在主区手工保存快照。

```powershell
# 只读：先确认空间标识、当前版本与历史版本完整ID。
$knowledgeSpaceSlug = 'llm-engineering-v1'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/knowledge/spaces'
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/knowledge/spaces/$knowledgeSpaceSlug/versions"

# 手动写操作：将占位符替换为上一步查到的历史版本ID，不是文档版本或build_id。
# 请核对后单独执行；不建议把本节整体复制运行。
$historicalSpaceVersionId = '<historical_space_version_id>'
Invoke-RestMethod -Method Delete "http://127.0.0.1:8000/api/v1/knowledge/spaces/$knowledgeSpaceSlug/versions/$historicalSpaceVersionId"
```

这是空间快照的逻辑删除，不释放原文或索引占用的磁盘空间。成功响应带 `deleted_at`，公开历史列表将隐藏该快照且不能再切换到它；内部审计保留，当前检索不变。接口没有提供撤销删除，误操作时不要直接修改生产数据库。版本编号不会因删除而被复用。

## 14. 中英混合查询扩展（v0.6.0补记）

默认开启术语级扩展，最多追加2条查询；原始问题始终保留。重启后端、刷新前端即可使用，不需重建索引或保存新的空间版本。前端「检索参数」中的开关和上限仅影响下一次显式运行的查询，不修改空间配置；请求未覆盖的参数及Agent工具使用.env服务默认。

```dotenv
# 服务默认：启用中英术语扩展，不增加LLM调用。
KNOWLEDGE_QUERY_EXPANSION_ENABLED=true
# 上限是额外查询数量，允许1–4；不是证据条数，也不包括原始问题。
KNOWLEDGE_QUERY_EXPANSION_LIMIT=2
# 可选：UTF-8 JSON词表覆盖/扩展内置术语。留空使用内置词表，路径放在E盘。
KNOWLEDGE_QUERY_ALIASES_PATH=
```

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'

# 运行正常后端后，显式执行一次仅关键词召回，比较中英术语扩展的效果。
# 该示例禁用向量与重排，不发出模型请求；请使用已有已发布文档的空间。
$bilingualSearchBody = @{
    space = 'llm-engineering-v1'
    query = '注意力机制的计算原理是什么？'
    result_limit = 5
    tuning = @{
        query_expansion_enabled = $true
        query_expansion_limit = 2
        vector_weight = 0
        rerank_enabled = $false
    }
} | ConvertTo-Json -Depth 5
$bilingualSearchResult = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/knowledge/search/debug' -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($bilingualSearchBody))
# 查看原始问题和实际扩展查询；未匹配词表或内容为空时可能只有原问题。
$bilingualSearchResult.trace.query_variants
$bilingualSearchResult.trace.matched_terms
$bilingualSearchResult.trace.degraded
```

自定义词表格式如下，在编辑器中保存为例如`E:\codex\agent\data\knowledge\query-aliases.json`，再将该路径写入.env并重启后端。相同`id`替换整条内置记录，新的`id`增加术语；每种语言第一项作为扩展用的正式短语，后续项作为匹配别名。文件应仅包含`terms`，每条必须有`id/zh/en`；跨术语的重复别名和无效格式会显式报错。不要将密钥或敏感信息放入术语表。

```json
{
  "terms": [
    {"id": "document_layout", "zh": ["文档版面分析", "版面识别"], "en": ["document layout analysis", "layout parsing"]}
  ]
}
```

词表不是自由翻译模型，未配置的领域需要自行补充别名。英文短语按词边界匹配，中文优先匹配更完整的术语；无可靠映射则只使用原问题。查询变体之间先按排名合并，关键词与语义原始分取变体最大值，融合分仍为名次分而非概率。开启向量召回时，会对实际查询列表进行一次批量Embedding；批量不可用时回退一次原查询Embedding，并在Trace中记录降级。关闭扩展恢复原单查询链路。

本地专项回归命令如下。显式指定pytest临时目录，避免系统目录不可写时回退到C盘；每次使用新的目录名。浏览器命令需先按第12节启动3210端口隔离前端，API仅使用测试样例。

```powershell
# 查询扩展及检索集成回归；无真实模型请求，不更改知识空间。
$queryTestStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
E:\conda_envs\langchain\python.exe -m pytest backend/tests/test_query_pipeline.py backend/tests/test_bilingual_retrieval.py -q --basetemp="E:/codex/agent/build/query-expansion-tests-$queryTestStamp" -o cache_dir=build/pytest-cache
# 前端显式提交、原问题/变体显示、开关和上限，以及旧版本管理回归。
node frontend/scripts/verify-knowledge-browser.mjs http://127.0.0.1:3210
```

## 15. v0.7.0：执行持久化、索引身份与评测

以下均在项目根目录执行。首次切换代码前先停止旧API和独立知识worker。执行表增加在原PostgreSQL数据库，原文、旧向量、空间快照与Run历史保留。

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:PIP_CACHE_DIR = 'E:\codex\agent\.pip-cache'
$env:PYTHONPYCACHEPREFIX = 'E:\codex\agent\.pycache'
# 使用现有E盘langchain环境安装官方checkpoint依赖。
E:\conda_envs\langchain\python.exe -m pip install -e '.[dev,studio]'
# 新增执行业务表；不重新解析或生成Embedding。
E:\conda_envs\langchain\python.exe -m alembic upgrade head
# 初始化官方Saver表，幂等；应用启动只验证结构。
E:\conda_envs\langchain\python.exe scripts/init_execution_store.py
# 首版单进程执行；Windows驱动适配已封装，无需改全局事件循环。
E:\conda_envs\langchain\python.exe -m uvicorn atlasflow.main:create_app --factory --host 127.0.0.1 --port 8000
```

新终端按原前端启动指令运行。访问`/knowledge/evaluations`查看评测，Run详情的“继续执行”恢复同一个中断Run；等待审批仍走审批面板。旧失败Run没有checkpoint时只能重新运行。

```powershell
# 零模型调用预检，默认前10题×2配置；缺冻结输入会明确返回缺失项。
E:\conda_envs\langchain\python.exe scripts/evaluate.py preflight
# 40题×6配置完整矩阵的离线输入检查。
E:\conda_envs\langchain\python.exe scripts/evaluate.py preflight --all-cases --all-arms
# 真实模式的服务端预检仍不发Provider请求。
E:\conda_envs\langchain\python.exe scripts/evaluate.py preflight --mode live --confirm-live
# 明确收费执行：60次业务HTTP尝试和20分钟上限，超限保存partial。
E:\conda_envs\langchain\python.exe scripts/evaluate.py run --mode live --confirm-live --max-http-attempts 60 --max-seconds 1200
# 真实结果采集后可自动匹配并离线重算。
E:\conda_envs\langchain\python.exe scripts/evaluate.py run --mode replay
# Agent题集默认回放；恢复样例需要实际重启后捕获的Run。
E:\conda_envs\langchain\python.exe scripts/evaluate.py preflight --dataset llm-agent-v1
# 替换实际实验ID；默认要求同索引，跨索引迭代须加--comparison-scope index。
E:\conda_envs\langchain\python.exe scripts/evaluate.py compare --baseline BASELINE_ID --candidate CANDIDATE_ID
E:\conda_envs\langchain\python.exe scripts/evaluate.py export --experiment EXPERIMENT_ID --format csv
# 以下检查遗留无绑定RUNNING任务，不启动worker、不调用模型。
E:\conda_envs\langchain\python.exe -m atlasflow.knowledge.cli repair-legacy-jobs --space llm-engineering-v1
# 确认旧API/worker已停止后才修复状态，保留原文。
E:\conda_envs\langchain\python.exe -m atlasflow.knowledge.cli repair-legacy-jobs --space llm-engineering-v1 --confirm-no-workers
# 当前索引身份未知时，重建仍需显式入队；本条仅预览。
E:\conda_envs\langchain\python.exe scripts/reindex_knowledge.py --space llm-engineering-v1
```

评测预算包含模型重试、高德内部调用、天气和网页重定向，不能保证整个批次完成；Token/成本缺失显示unknown。独立Judge默认关闭，启用还需确认额外调用。LangSmith仅同步实验指标与元数据，完整输入/报告留在E盘。

本地详细代码与流程说明：`docs/v0.7.0-runtime-and-evaluation.md`。docs和data/evaluations被Git忽略，README仅追加本版变更。

2026-10-14本机已完成`0004_execution`迁移及Saver初始化，上述两条初始化命令可重复执行。新表位于现有数据库的`atlasflow_execution`schema，VSCode中请刷新并展开该schema。原知识数据、空间指针和Run历史未改变；未执行真实评测或重建。

2026-10-15检查时Docker引擎未运行。如果API或PG专项连接拒绝，请先启动Docker Desktop，等待引擎就绪，再在项目根目录按原指令执行`docker compose up -d postgres`。这与checkpoint迁移或知识索引版本无关，不要为连接问题重建空间。需要严格将Docker CLI配置也放在E盘时，可在该终端设置`$env:DOCKER_CONFIG='E:\codex\agent\.docker'`；Docker Desktop的现有磁盘位置无需变动。

```powershell
# 前端新增的评测/恢复浏览器回归；仅连接隔离前端并拦截API。
# 先在专用终端进入frontend，设置独立distDir并运行3212端口dev。
Set-Location E:\codex\agent\frontend
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:ATLASFLOW_NEXT_DIST_DIR = '.next-evaluation-check'
npm run dev -- --port 3212
# 另一终端同样进入frontend后执行；关闭测试服务再恢复环境变量。
npm run test:evaluation:browser -- http://127.0.0.1:3212
# 仅在原测试终端停止dev后执行，恢复正式开发/启动的默认构建目录。
Remove-Item Env:ATLASFLOW_NEXT_DIST_DIR
```

## 16. v0.7.0补记：独立评测v2与组间对照

本节取代第15节中“使用统一评测页面”的当前入口说明，历史命令不删除。RAG进入`http://localhost:3000/evaluations/rag`，Agent进入`http://localhost:3000/evaluations/agent`；原`/knowledge/evaluations`只读查看v1历史。版本仍为0.7.0，v2是协议号。

先按第15节启动原PostgreSQL、API和前端。仅本次评测重构无需新的Alembic迁移或空间重建；后端启动会在原独立评测SQLite中幂等创建v2两张表。若数据库连接未就绪，先解决Docker/PG启动问题，不要因此重建语料。

以下CLI调用**已经运行的API**，不在命令里启动应用或知识worker。默认模板为replay；初次没有匹配的完整冻结产物时，预检会报缺失输入，而不是生成虚假结果。

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:PYTHONPYCACHEPREFIX = 'E:\codex\agent\.pycache'

# 只读：查看各领域题集、预算、Judge状态；Agent设置包含真实工具目录。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py datasets --kind rag
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py settings --kind agent

# 在编辑器中核对两个模板的case、空间和工具列表。默认replay、Judge关闭、不联网采集。
# RAG模板只改变重排开关；Agent模板固定模型/策略，只改变工具白名单。
# 预检不发送Provider请求；返回非0且缺少冻结输入时，需要先显式采集真实输入。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py preflight --kind rag --plan E:\codex\agent\evals\rag\plan-v2.example.json
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py preflight --kind agent --plan E:\codex\agent\evals\agent\tool-comparison-v2.example.json

# 有完整匹配输入、预检通过后执行回放；不会重新生成回答/Agent轨迹。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py run --kind rag --plan E:\codex\agent\evals\rag\plan-v2.example.json
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py run --kind agent --plan E:\codex\agent\evals\agent\tool-comparison-v2.example.json

# 只读：列出实验；run返回实验ID，状态可继续查询，命令不会等待整批执行完成。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py list --kind rag
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py show --kind rag --experiment EXPERIMENT_ID

# 同实验组间对照：替换真实实验ID及该实验中的基线variant_id。
# 只对同case/trial的可评结果配对，缺标注、降级与失败单独统计。
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/evaluations/rag/experiments/EXPERIMENT_ID/comparison?baseline_variant_id=baseline'

# 跨实验比较：RAG调参用retrieval；整空间索引迭代用index；Agent工具对照用agent。
# 协议、题集/审核标签、语料与非干预变量不同会拒绝比较，不强行求差。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py compare --kind rag --scope retrieval --baseline BASELINE_ID --candidate CANDIDATE_ID
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py compare --kind agent --scope agent --baseline BASELINE_ID --candidate CANDIDATE_ID

# 导出默认打印到终端；需要文件时优先使用页面下载并保存到E盘。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py export --kind rag --experiment EXPERIMENT_ID --format csv
# 写操作：明确取消该v2实验，不取消生产Run；核对ID后单独执行。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py cancel --kind agent --experiment EXPERIMENT_ID --confirm-cancel
```

真实采集建议从页面完成：先选择少量题、两组配置与费用上限，再选择live、预检并确认。使用CLI时，应在编辑器另存E盘计划，明确改为`mode: live`及`confirm_live: true`，再提交；不要盲目把以上默认回放命令改成收费批量运行。独立Judge还需单独启用配置，并在计划中同时设置`judge_enabled`、`confirm_judge`，会额外发送回答与证据且消耗预算。纯检索协议拒绝Judge，因为没有回答可评。

Agent工具实验使用`comparison_focus: tools`；固定模型、RunPolicy、工作流、提示词，只调整两组工具白名单。`null`表示完整目录，`[]`表示没有工具；配置缺失和实验禁用是不同状态，目录“配置就绪”不代表已联网验证。默认单题600秒、并发1；一次超时只终止该单元，全局HTTP/时限仍可停止整批。现有A05/A06/A08较适合知识/联网组合；没有地图/天气专用题时不要声称验证了这些工具的收益。

```powershell
# 离线后端回归：显式临时目录和缓存位于E盘；可选PG专项仍需另行启用。
$evalV2TestStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
E:\conda_envs\langchain\python.exe -m pytest backend/tests -q --basetemp="E:/codex/agent/build/evaluation-v2-tests-$evalV2TestStamp" -o cache_dir=build/pytest-cache-v2
E:\conda_envs\langchain\python.exe -m ruff check backend scripts evals

# 浏览器回归：专用终端启动隔离前端，不覆盖正常.next。
Set-Location E:\codex\agent\frontend
$env:ATLASFLOW_NEXT_DIST_DIR = '.next-eval-v2-check'
npm run dev -- --port 3214
# 另一终端同样进入frontend后运行；脚本拦截API，不触发真实模型。
npm run test:evaluations:v2:browser -- http://127.0.0.1:3214
# 仅在停止本专用dev进程后清除临时变量。
Remove-Item Env:ATLASFLOW_NEXT_DIST_DIR
```

页面主结果以配对差值为准，比例/评分保留原值的绝对变化（如0.500→0.600为+0.100，非相对增幅）；耗时和费用保留对应单位，耗时按越低越好解释。没有已审核参考事实、qrels或Agent语义rubric时，相关质量指标显示未评测，不补0、不冒充质量已提升。详细实际代码与流程见本地`docs/evaluation-v2-implementation.md`；该目录仍不提交Git。

## 17. 评测指标图形对照（v0.7.0补记）

本次仅前端展示增强，已运行的后端无需迁移或重建知识库；刷新前端开发页面，或重新构建并启动前端生产服务即可。打开RAG/Agent实验记录，在结果区点击指标，再选择基线与候选组。均值柱图比较同一批配对样本，逐题图以0为中线显示差值；下方解释会说明方向、可评覆盖与排除原因。缺标注时应先补齐评测依据，不要把空图当成0分。

```powershell
# 项目原E盘前端目录；保持缓存和临时产物在E盘。
Set-Location E:\codex\agent\frontend
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
# 不联网：图表配对子集、轴域、0/null、单位、方向和解释的确定性检查。
npm run test:evaluations:chart
npm run typecheck

# 可选浏览器验收，专用终端启动；不要覆盖正常.next。
$env:ATLASFLOW_NEXT_DIST_DIR = '.next-eval-chart-check'
npm run dev -- --port 3216
# 另一终端进入相同frontend目录后运行；API与远端请求由脚本拦截。
npm run test:evaluations:v2:browser -- http://127.0.0.1:3216
# 仅在停止本专用dev后清除变量。
Remove-Item Env:ATLASFLOW_NEXT_DIST_DIR
```

图表解释不调用LLM，不会自动发起新实验、重跑模型或消耗Judge预算。两组相同均值可能来自每道题都相同，也可能是改善/退化互相抵消，应结合逐题图判断；低耗时也不单独代表更高任务质量。

## 18. 多组同图、详情弹窗与历史入口移除（2026-10-16）

本节更新第17节的界面操作，原命令继续适用。打开`/evaluations/rag`或`/evaluations/agent`，选择一条v2实验记录，再点击指标；所有配置组会同时显示，不再选择基线/候选。各组共用同一批可评题目与重复编号，均值与逐题表现使用统一值轴，空值不画成0。

逐题记录中点击「查看详情」打开弹窗，按关闭按钮、Esc或遮罩退出。关闭后焦点回到原按钮，读取失败时可查看已保存评分；原页内详情和「核对数据」模块不再出现。「历史实验」入口已移除，旧`/knowledge/evaluations`自动跳转RAG实验；没有删除历史数据库记录。

这次只改前端与展示模型，无需Alembic迁移、重建空间或重新运行实验。开发环境刷新前端即可；生产环境按原命令重新构建、启动。回归仍使用第17节的`test:evaluations:chart`、`typecheck`与3216隔离浏览器命令，包含多组共同分母、弹窗焦点/迟到请求、旧入口跳转、移动与深色视图。


## 19. RAG仅真实执行与实验记录删除（2026-10-16）

本节更新第18节的运行方式。RAG实验页直接按当前配置执行，不再选择冻结回放与冻结输入；Agent实验保持原有真实执行/回放。查看旧实验产物不等于重新执行，仍然不产生模型调用。页面发起新实验前必须预检、确认调用预算；切换参数后重新预检。回答协议可额外启用独立Judge，单独确认费用。

以下均为VSCode PowerShell终端命令；先正常启动后端，命令本身不要整段无差别执行。

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'

# RAG示例现在为mode=live、confirm_live=false。
# 仅检查题集、空间、参数与服务就绪情况，不调用模型。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py preflight --kind rag --plan E:\codex\agent\evals\rag\plan-v2.example.json

# 真正执行前，复制/编辑E盘计划并明确将confirm_live设为true，核对预算与空间。
# 默认示例未经确认时，此命令会在发出HTTP之前拒绝；执行可能产生费用。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py run --kind rag --plan E:\codex\agent\evals\rag\plan-v2.example.json

# 分别列出RAG/Agent实验，取得准确的experiment_id。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py list --kind rag
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py list --kind agent

# 将下方占位符替换成“确实准备删除”的实验ID；不要使用Run ID、空间版本ID或Trace ID。
$experimentId = '替换为实验ID'

# 可选：先导出需要保留的结果；CLI默认输出到终端。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py export --kind rag --experiment $experimentId --format json

# 删除指定RAG实验记录；没有--confirm-delete时不会发送请求。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py delete --kind rag --experiment $experimentId --confirm-delete

# 删除Agent记录时改用--kind agent，必须核对该领域的准确ID。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py delete --kind agent --experiment $experimentId --confirm-delete

# 同等HTTP接口示例（二选一）；成功返回status=deleted、deleted_at、artifacts_retained=true。
Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:8000/api/v1/evaluations/rag/experiments/$experimentId"

# 若返回409，先等待结束；确实需要停止时，显式取消后再删除，不自动取消。
E:\conda_envs\langchain\python.exe scripts/evaluate_v2.py cancel --kind rag --experiment $experimentId --confirm-cancel
```

删除范围仅为指定领域的v2实验记录：成功200，未知或跨领域ID为404，仍执行/收尾为409。同领域重复删除返回同一删除时间，删除后的GET、导出、产物与对比为404；列表也不再包含它。当前只提供接口、CLI和前端API客户端方法，没有新增删除按钮。软删除保留原始文档、切块、向量、共享评测证据与LangSmith追踪，不释放磁盘，不提供恢复/物理清除接口；已被其他Agent实验引用的证据不受影响。

存储沿用原评测SQLite的`evaluation_v2_experiments.payload`，新增`deleted_at`标记，无需PostgreSQL/Alembic迁移，也不用重建知识空间。重启后端以注册新接口，开发前端刷新即可；生产前端按原指令重新构建。旧版v1兼容API暂保留，本轮变更针对独立v2实验工作区。

```powershell
# 离线后端回归：全部使用临时记录和测试Provider，不删除现有记录。
# basetemp仅使用本轮专属测试目录，勿改成数据目录。
$env:PYTHONPYCACHEPREFIX = 'E:\codex\tmp\pycache'
E:\conda_envs\langchain\python.exe -m pytest backend/tests/test_evaluation_v2_delete.py backend/tests/test_evaluation_v2_cli.py backend/tests/test_evaluation_v2_common.py backend/tests/test_evaluation_v2_integration.py backend/tests/test_rag_evaluation_v2.py --basetemp=E:\codex\agent\build\pytest-eval-delete-manual -p no:cacheprovider -q
```


## 20. 前端删除实验记录与比较模块调整（2026-10-16）

本节补充第19节：现在RAG实验和Agent实验都可直接在「实验记录」列表操作删除，无需手动调用接口。

1. 打开`/evaluations/rag`或`/evaluations/agent`，找到准确的实验记录；每行末尾始终显示「删除」，与点击查看结果分开。
2. 运行中的记录不可删除。若要终止，先选中记录，在结果区点击「停止实验」并确认，等待状态更新后再删除；后端仍在收尾时返回409，稍后重试，不会替你强制取消。
3. 点击「删除」，核对弹窗里的协议与完整实验ID，确认数据保留说明，再点击「确认删除」。按「返回」或Esc取消，不发送删除请求。
4. 成功后该行从列表移除，并显示提示；删除当前选中记录时一并清空结果与详情，不会自动选中其他记录。删除其他记录时当前结果保持不变。失败时记录与结果保留，可按错误原因重试。

删除仍是软删除：保留原文、索引、Embedding和共享证据，不删除LangSmith追踪，也不释放磁盘。当前页面不提供恢复操作。确认过程中不能重复删除，成功后焦点回到「实验记录」标题；刷新后已删除记录不再出现。

已移除两个页面独立的「配对比较」区域，不再选择基线实验/候选实验。单次实验内的所有配置组同图比较、逐题图、详情弹窗及下载继续保留，不改变评分标准或指标分母。后端比较API保留兼容，本次不迁移数据库，也不改实验数据。开发前端刷新即可，生产环境按原指令重新构建；后端须包含第19节新增的DELETE接口。

回归命令仍使用第18节的前端检查和3216隔离浏览器流程。浏览器脚本只删除内存夹具，覆盖取消、重复点击、409/500失败保留、删除当前/非当前记录、迟到响应不复活、刷新持久化、最后一条空态，以及桌面/手机/深色主题；不删除真实实验记录。


## 21. 主模型复用与下拉选择（2026-10-16）

当前Agent与RAG回答默认使用`LLM_MODEL="~deepseek/deepseek-flash-latest"`。Judge模型、Key、BaseURL留空时也复用主模型与连接，不必复制相同模型名。保留当前`.env`的Judge开关；服务端开启代表可用，前端仍须勾选独立评审并确认调用，才会产生额外评审请求。

```dotenv
# 当前主模型；API密钥与BaseURL沿用已有配置，此处不展示。
LLM_MODEL="~deepseek/deepseek-flash-latest"

# 可选的额外聊天模型ID，必须是JSON数组；空数组表示只提供主模型。
# 不填写Embedding或Rerank模型，不填写服务URL。
LLM_MODEL_OPTIONS=[]

# 当前已由用户开启；不会自动启动评审或实验。
EVALUATION_JUDGE_ENABLED=true
# 留空跟随LLM_MODEL；以后需要独立模型时再显式填写。
EVALUATION_JUDGE_MODEL=
EVALUATION_JUDGE_API_KEY=
EVALUATION_JUDGE_BASE_URL=
```

打开RAG实验并选择「检索与回答」，在「回答模型」下拉选择；Agent实验使用「共同执行模型」下拉，两组共享同一选择。默认项会显示实际主模型名，不再手工输入。当前只配置一个主模型，所以只有一个选项，这是正常状态；以后在`LLM_MODEL_OPTIONS`中添加同一模型服务支持的候选ID，重启后端、刷新页面即可出现，无需改前端代码。

Judge区域同时显示固定的评审模型；选择其他回答/执行模型不会改变服务端Judge配置。修改模型后旧预检失效，需重新检查条件。候选只说明已配置，不保证远端连通、格式或工具调用兼容；实际调用失败仍按Provider错误记录。

重启后端以加载新代码与`.env`，开发前端刷新；生产前端按原指令重新构建。默认复用不改变独立评审的调用次数和费用，也不改变Embedding、Rerank与知识空间索引，不需要重建或迁移数据库。同模型评审存在同源偏差，后续可分别配置模型。

```powershell
# E盘隔离测试，仅使用测试Provider，不产生模型费用。
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:PYTHONPYCACHEPREFIX = 'E:\codex\tmp\pycache'
E:\conda_envs\langchain\python.exe -m pytest backend/tests/test_evaluation_model_reuse.py backend/tests/test_rag_evaluation_v2.py backend/tests/test_evaluation_v2_integration.py --basetemp=E:\codex\agent\build\pytest-model-reuse-manual -p no:cacheprovider -q
```

## 22. 实验界面选项精简与错误解释（2026-10-16）

两个实验页不再显示「同步LangSmith摘要」，RAG页也不再显示「允许降级诊断」。新网页实验固定不主动同步摘要、不允许降级执行；不改变后端API能力或已有全局LangSmith追踪设置。索引身份未知、Embedding不兼容或服务不就绪时仍按原预检/执行规则阻止或报告错误，不因移除开关而跳过安全检查。历史结果仍保留原来的同步状态、降级说明与证据。

开发前端刷新即可，生产环境按原命令重新构建；本次没有模型配置、数据库迁移或知识空间重建。独立Judge、模型下拉、预算、删除确认与单次实验内多组对比继续保留。浏览器回归额外校验控件不存在，以及预检/创建请求中两个字段均为false，不请求真实Provider。

最新实际实验的完整诊断位于`E:\codex\agent\docs\latest-rag-experiment-diagnosis-2026-10-16.md`。执行失败与Judge失败是两个阶段：前者表示答案没完整生成，后者可能发生在已生成答案上；HTTP200不代表正文或评分JSON完整，missing_gold也不是Provider故障。本次只检查原因，没有更改Token参数、增加自动重试或重跑实验。

## 23. 实验检索参数区去除重复开关（2026-10-16）

RAG实验右侧参数区不再显示「启用重排」「中英查询扩展」及其只读提示，功能是否启用继续由左侧各检索对照组决定，不需要再次勾选。重排候选与扩展查询上限保留，未选启用相应功能的组时仍按原规则禁用该参数。知识库单次检索页的两个可编辑开关保持不变。

开发前端刷新即可，生产前端按原指令重新构建，无需重启后端、重建空间或重新跑实验。此次只精简展示，没有改变重排/扩展功能、模型或评测预算。新增离线检查命令：

```powershell
Set-Location E:\codex\agent\frontend
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
# 渲染共享控件：实验模式无重复开关，单次查询保留可调开关；无真实API。
npm run test:retrieval:controls
# 类型及图表检查；浏览器回归沿用第18节的隔离服务流程。
npm run typecheck
npm run test:evaluations:chart
```

## 24. 回答与Judge预算、参考答案标注（2026-10-16）

本节描述新增行为，不覆盖前面各版记录。项目版本仍为0.7.0；RAG题集版本升级到2.1，并不代表项目升级到2.1。

```dotenv
# 单次Judge请求的输出上限；256—16384，默认4096，不再硬编码2200。
# 更高上限可能增加耗时/费用，不改变整个实验的HTTP或时间预算。
EVALUATION_JUDGE_MAX_TOKENS=4096
# 每个评分项的简短依据长度；40—600字符，默认160，不限制生成报告的长度。
EVALUATION_JUDGE_MAX_REASON_CHARS=160
```

以上两项已写入本地.env与.env.example。原有主模型、密钥、Judge启用状态及专用连接回退策略保持不变。修改后重启后端；开发前端刷新，生产前端按前面的构建命令重新构建。没有自动启动Judge；创建实验仍须人工确认真实调用及额外评审费用。

RAG实验选择「检索与回答」后，回答Token上限默认4096，可在128—4096范围内调整；上下文字符上限仍为12000，可在1000—40000范围内调整。这是输出上限，不要求写满。新的rag-answer-v2.1提示词要求聚焦问题，通常控制在500个汉字或250个英文词以内；工作流合稿报告不受此长度建议影响。Judge每个评分项只返回一句必要依据，服务端对超长理由限长并保留judge_reason_shortened诊断；length截断继续记录为失败，不截取半份JSON评分。

新增evals/rag/reference-answers-v2.json，当前只补R01、R04，与上一轮实际实验的两道题对应。参考事实由本地原论文核对，包含文档SHA256、页码、章节、逐条事实对应锚点、核对日期与Codex整理标记；不是伪造的人工审核记录。其他38题继续缺标注，不能据此推断全部题目的正确性评分可用。原始evals/rag/dataset.json不变，没有修改语料、切块或Embedding。

参考答案不会放入回答生成器输入。评分器仍在缺少已核验参考事实时保留missing_gold；补标注后的新实验会使用新数据集哈希，旧实验不自动重评。旧实验标签与当前题集哈希不一致时，页面明确提示不展示当前参考标注，历史分数与产物保留。提高预算主要降低原先length截断风险，不保证远端Provider永不失败；请重新创建一轮小规模实验观察，而不是把旧失败记录当作已修复结果。

```powershell
Set-Location E:\codex\agent
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
$env:PYTHONPYCACHEPREFIX = 'E:\codex\tmp\pycache'
# 完全离线：预算边界、截断防护、引用来源核对、旧标签隔离；不重跑真实实验。
E:\conda_envs\langchain\python.exe -m pytest backend/tests/test_rag_answer_quality.py backend/tests/test_evaluation_model_reuse.py backend/tests/test_evaluation_v2_common.py --basetemp=E:\codex\agent\build\pytest-rag-quality-manual -p no:cacheprovider -q
```

本轮已完成离线验收：后端565通过、5跳过；前端类型、控件、图表、流程、报告、生产构建与154次浏览器夹具请求通过。没有产生真实调用费用，没有改写旧实验或重建知识空间。截图与诊断在build/evaluation-v2-browser/check-oXGtws；详细代码说明见本地docs/evaluation-v2-implementation.md第13节。

## 25. 评测主指标与多配置差距（2026-10-16）

本次只调整主指标及展示，不需要数据库迁移、知识空间重建或重跑历史实验。开发前端刷新，生产前端按原指令重新构建并重启前端服务；后端接口和评分公式保持不变。新网页RAG实验使用固定cutoffs=[1,5]，返回片段数至少5；不再勾选@3/@10。旧记录没有采集的主指标不补算、不填0，保留已有原始字段。

| 实验协议 | 主指标数 | 保留内容 |
| --- | --- | --- |
| RAG证据检索 | 5 | child覆盖@5、首位命中@1、MRR@5、上下文覆盖@5、检索耗时 |
| RAG检索与回答 | 8 | child覆盖@5、首位命中@1、MRR@5、参考事实覆盖、正确性、事实支持、引用支持、检索耗时 |
| Agent任务场景 | 8 | 任务成功、任务输出评分、工具约束通过、工具失败数、执行次数、耗时、已知Token、已知费用 |
| Agent工程契约 | 7 | 轨迹约束通过、工具约束通过、工具失败数、执行次数、耗时、已知Token、已知费用 |

结果区先展示各组并列数值和组间最大差距，点击指标名称即可更新原有图表；支持Tab/Enter。手机端可横向滚动各配置列，指标名称固定。每行的共同可评数量可能不同，缺少全组交集时不显示伪造的均值。50%到70%的差距为20个百分点，不是20%相对提升；MRR从0.25到1.00的差为0.75，不解释成75个百分点。

均值持平仍正常显示；无标注、Judge失败或未采集不当作0。引用ID合法与流程完成继续是原始诊断，不是答案正确或任务成功。新界面没有增加任何模型调用，下载JSON/CSV仍可查看其他完整诊断字段。2题的小样本不能证明哪种配置普遍更好；本次不自动添加未核验题目、不重跑付费实验。

```powershell
Set-Location E:\codex\agent\frontend
$env:TEMP = 'E:\codex\tmp'
$env:TMP = 'E:\codex\tmp'
# 离线回归固定指标清单、百分比/百分点、共同样本与缺评保护。
npm run typecheck
npm run test:evaluations:chart
# 常规生产构建，非启动后台服务；隔离浏览器验证沿用第18节。
npm run build
```

本轮已通过51项后端相关测试、前端离线回归和生产构建；浏览器165次拦截请求、0异常，截图在build/evaluation-v2-browser/check-a5E5en。只验证测试Provider与内存夹具，不写真实实验库，不产生模型费用。所有临时文件均在E盘，3216验收服务已停止。
