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
