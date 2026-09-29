# 工具能力规划与扩展指南（v5.0）

本指南对应 2026-09-27 的能力规划改造。历史 README 中的 `ROLE_TOOLS` 和 Researcher 语义正则已被替代；RAG 仍不在当前 Agent 工具阶段中。

## 1. 现在的职责分工

```text
工具文件声明 Capability、参数 Schema、角色、可用状态和留存规则
                      ↓
                 ToolRegistry
                      ↓ 动态能力目录
用户问题 → Planner → ResearchTask → 审批 → Researcher 工具阶段
                        │                     │
                        │                     ├─ 必需能力约束
                        │                     ├─ LLM 选择具体实现及调用参数
                        │                     └─ LLM 按需追加其他工具
                        │                              ↓
                        │                         ToolRuntime
                        │                   预算、权限、去重、事件、证据
                        │                              ↓
                        └───────────────────── ResearchResult
                                                       ↓
                                            Critic 审查成功标准
                                         Accept / Supplement / Replan
```

Planner 本身仍有可选工具阶段；Critic、Synthesizer、Quality Gate 同样可以按需调用其角色允许的工具。上图聚焦研究任务的能力要求，不表示其他 Agent 被禁用工具。

## 2. 任务契约

在原 `ResearchTask` 上新增，不另建 `PlanTask`：

```json
{
  "task_id": "T1",
  "title": "获取美元兑人民币报价",
  "objective": "查询当前 USD/CNY，明确数据时间和报价方向",
  "success_criteria": ["给出可核对来源 URL、报价方向、数值和数据时间"],
  "priority": 1,
  "dependencies": [],
  "requires_fresh_data": true,
  "required_capabilities": ["web_search"],
  "plan_version": 1
}
```

- Planner 和 Critic 生成的新任务必须显式给出两个新字段；遗漏、未知能力、重复能力、不一致的新数据要求会触发结构化输出修复。
- 旧记录缺字段时默认 `false` / `[]`，只保证历史读取兼容，不会用关键词重新推断历史任务意图。需要新策略时创建新 Run。
- 能力 ID 是受格式约束的字符串，不是源码里固定的 `Literal` 清单。模型的 JSON Schema 枚举由注册表生成。
- `requires_fresh_data=true` 至少需要一个标记为 `supports_fresh_data=true` 的能力；不能仅声明计算器。
- “重新获取数据”不等于“来源数据一定实时”：网页可能引用旧数据。Researcher 必须检查来源日期，Critic 仍要审查时效、相关性和成功标准。
- 暂时不可用但已知的能力可以进入计划，例如未配置高德 Key。执行时明确失败，不把要求删掉或用模型记忆假装完成。

## 3. 能力与工具是两层

`Capability` 表达任务需要什么；`BaseTool.name` 表达具体调用哪个实现。当前几个能力与工具名称恰好相同，但两者不要求同名。

例如能力 `weather_lookup` 可以同时由 `weather_provider_a`、`weather_provider_b` 提供。模型只规划 `weather_lookup`；工具阶段从当前角色可用的实现中选择。单一实现时使用指定函数的 `tool_choice`；多个实现时只提供该能力候选集并使用 `tool_choice="required"`。要求满足后恢复可选工具选择。

同一个能力 ID 的描述和新数据标记必须一致；冲突注册会被拒绝。替代实现应复用同一个 `Capability` 定义。不会自动扫描或加载磁盘上的任意 Python 文件，注册仍是显式、可审查的。

## 4. 新增一个普通只读工具

1. 在 `backend/src/atlasflow/tools/` 新建一个 `.py` 文件，放该工具参数模型、工具类和专属方法。
2. 声明 `name`、`description`、`arguments_model`、`capabilities`；按需覆盖 `allowed_agents`、`risk_level`、`unavailable_reason()`。
3. 实现异步 `run()`，返回 `ToolResult`。事实查询应返回带真实来源与数据时间的 `Evidence`，不要把失败包装成成功。
4. 在 `tools/catalog.py` 的 `build_tool_registry()` 中注册一次，FastAPI 与 Studio 共用这个入口。依赖由构造器注入；配置读取自 Settings / `.env`，不在文件内硬编码 Key。
5. 添加成功、失败、参数、配置缺失、角色权限、留存和集成测试。

接口示意（不是已经接入的天气服务）：

```python
from pydantic import BaseModel, ConfigDict, Field
from atlasflow.tools import BaseTool, Capability, ToolContext, ToolResult

WEATHER = Capability("weather_lookup", "查询指定城市的天气观测及数据时间", True)

class WeatherArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str = Field(min_length=1, max_length=80)

class WeatherTool(BaseTool):
    name = "weather_provider_a"
    description = "查询指定城市天气，返回来源和观测时间"
    arguments_model = WeatherArguments
    capabilities = (WEATHER,)
    allowed_agents = ("planner", "researcher", "critic")

    def __init__(self, client):
        self.client = client

    async def run(self, arguments: WeatherArguments, context: ToolContext) -> ToolResult:
        # 由你实现实际 Provider 适配器，并将结果转换为 ToolResult / Evidence。
        return await self.client.query_weather(arguments.city)
```

普通工具不需要修改 `workflow.py` 的语义分支、Gateway 的固定枚举或前端能力清单。如果新增的是写入、支付等高风险工具，还必须另外设计明确的人类授权流程；现有 Agent 工具阶段只开放低风险工具。

## 5. 工具专属留存规则

规则留在工具自己的文件中，运行器统一调用：

| 扩展点 | 用途 |
| --- | --- |
| `unavailable_reason()` | 本地配置检查，返回缺少配置的原因，不返回 Key，不发网络请求 |
| `safe_arguments()` | 进入事件和调用记录的参数；敏感工具必须覆盖并测试 |
| `safe_evidence()` | 可进入状态、历史和报告的证据 |
| `safe_summary()` | 可持久化的结果摘要；通用默认读取 `data.summary` |
| `format_summary()` | 后续 Agent 使用的摘要格式，例如算式和结果 |
| `navigation_url()` | 持久化导航入口，没有则返回 `None` |
| `model_calls_per_execution` | 工具实现内部固定发生的模型请求次数，默认 0 |
| `cache_identical_calls` | 只读工具可选择阶段内相同参数去重，默认关闭 |
| `transient_output` / `transient_notice` | 临时结果及使用限制，不进入后续结构化生成 |

高德继续只保存状态、模式和导航链接，不持久化距离/耗时；已有 LangSmith 地图脱敏测试保留。新增有特殊隐私要求的工具时，还须审查 `observability` 中的 Trace 输入/输出过滤，不能只改持久化钩子就假设 Trace 自动安全。

临时结果会关闭本阶段后续工具调用，以免模型把受限数据复制进下一次参数。必需能力会尽量先处理非临时工具；若任务需要多次相互独立的临时查询，建议规划为分开的任务，而不是假设一个阶段无限连续调用。

## 6. 复用边界与失败语义

- 非新数据任务可在重试时使用同一 `task_id`、同一 `plan_version`、同一 Agent 的成功能力记录。其他任务、旧版本和没有能力字段的旧记录不能替代必需执行。
- 当前阶段内，计算器和网页搜索对参数模型校验后的完全相同参数进行去重。地图、失败结果、默认未开启缓存的新工具不会缓存；跨阶段、跨任务、跨 Run 不共享该缓存。
- 重复请求仍占一个请求预算槽，避免无限循环；复用记录用 `reused_from_call_id` 标明来源，不计内部 Provider 调用，耗时记 0。预算指标统计调用请求，不等同实际外部请求次数。
- 新数据任务不以先前阶段的成功记录满足要求；同阶段的瞬时去重仍然有效。
- 必需能力没有成功结果时，Researcher 进入既有重试／失败流程，不能把此任务当作成功；整体仍受 quorum、依赖和审查策略控制，不保证任何一个任务失败都让整轮立刻终止。
- “成功调用过此能力”只是执行条件，不是充分证据的保证。多个来源、多个币种等覆盖要求由任务成功标准、Researcher 和 Critic 判断。

## 7. 查看与调试

- `GET /api/v1/tools`：具体工具、参数 Schema、允许角色、能力 ID 和本地配置状态。
- `GET /api/v1/capabilities`：Researcher 能力目录、可用实现、是否支持新数据和不可用原因。这里只反映本地配置与授权，不保证远端服务在线或模型支持调用。
- 网页审批面板可展开动态能力目录，编辑能力 ID 和新数据要求；任务抽屉展示这些要求，SSE 中的新任务也会保留字段。
- 非法人工能力编辑返回 HTTP 422，Run 保持等待审批；Studio 的直接审批恢复也做能力校验。
- 自动化回归在 `backend/tests/test_capability_planning.py`、`test_tool_runtime.py`、`test_api.py`。测试天气工具仅为测试替身，不是运行模式，不消耗真实 API 额度。

启动方式和 `.env` 无新增要求，修改代码后重启 FastAPI 与 Studio，再创建新 Run。版本保持 `5.0.0`。

## 8. 后续修补：规划边界与多段导航

以上章节保留改造历史，以下约束优先于前文关于 Planner 和 Synthesizer 的描述：

- Planner 默认不调用工具；开启 `RunPolicy.planner_allow_research` 后，每次规划最多一次调用，并要求工具声明 `planning_safe=True`。普通新工具默认不是规划调研工具。地图工具禁止 Planner 调用。
- Synthesizer 只复用已有证据；新增工具通常开放 Researcher、Critic 或 Quality Gate，不要为了增加调用量而扩大角色权限。
- Gateway 的用户问题参数始终保持原文，工具证据使用 `tool_context`，工具决策必须收到依赖任务结果。
- 新增 `MapItineraryArguments / MapItineraryTool`，文件为 `tools/map_itinerary.py`，能力为 `map_itinerary`。2–6 个有序站点、最多 5 段，返回起点时末尾重复起点。一处注册同时影响 FastAPI 和 Studio。
- `BaseTool.navigation_urls()` 支持多个持久化导航链接；旧 `navigation_url` 保留兼容。部分路段成功不代表整个工具成功。
- `AmapRequestLimiter` 由共享注册入口注入两个地图工具。单进程内节流不能协调其他进程或其他应用对同一 Key 的使用，因此仍保留次数有限的限流重试。
- 单路线成功后不再进行后续模型调用，多站点工具不会把距离／耗时传出。新增 `provider.openrouter.chat` Trace 可查看每次模型响应的结束原因；不要为了观测而记录受限制的地图数据或 Key。
- 地点名称只做显式等价规范化，不自动选择真正歧义的候选。新增别名需有明确语义依据及唯一性测试。

## 9. v5.0 后续增量：网页深读、天气、地点核对（2026-09-28）

三个工具各占一个文件，通过现有 `tools/catalog.py` 统一注册，不添加工作流里的固定工具名分支。FastAPI 与 Studio 都使用同一注册入口；更改后重启服务并创建新 Run。Planner 默认仍不调用工具，三个新增工具均不允许 Planner 调用。

| 能力 / 工具 | 数据来源 | 可保留内容 | 使用边界 |
| --- | --- | --- | --- |
| `web_fetch` | OpenRouter 的 `openrouter:web_fetch` server tool | 带目标 URL 的引用和限长摘录；若服务只返回模型概括，标为 `provider_synthesis` | 必须有与请求 URL 一致的引用，否则失败；只访问公开 HTTP(S) URL，单次最多抓取 1 个 URL |
| `weather_forecast` | Open-Meteo / GeoNames | 预测日期、温度、降水概率、查询时间、来源与署名 | 默认免费接口仅适合非商业演示；商业部署需订阅并配置 `OPEN_METEO_API_KEY`；预测不等于实测 |
| `poi_details` | 高德 POI 2.0，复用 `AMAP_API_KEY` | 仅工具调用状态与“需查场馆官网”的安全摘要 | 高德营业字段当次使用，不进 RunRecord、Event、报告或 LangSmith 输出；平均消费不等于门票价 |

推荐任务拆分：先 `web_search` 找场馆官网与来源，再 `web_fetch` 深读开放时间／预约页；另用 `weather_forecast` 取得可署名的短期天气，`poi_details` 仅辅助检查地点是否存在。地点名称不唯一时请补充城市与全称，不能自动选第一个搜索结果。页面中的文字一律按不可信资料处理，不能作为新指令执行。

天气 API 默认不需要 Key；`OPEN_METEO_API_KEY` 仅用于已订阅的商业 API。高德 Key 与 Open-Meteo Key 均只从配置注入，不写到工具文件；安全测试检查结果和 LangSmith 脱敏。Open-Meteo 资料需在报告中署名，并注明查询时间。高德许可限制保存其 POI 和天气等数据，因此本项目不把高德 POI 字段做成可引用的持久化证据；营业、预约和门票以官方场馆页面为准。

新增离线测试在 `backend/tests/test_new_tools.py`。本轮没有真实服务联调；OpenRouter server tool、Open-Meteo 和高德 Key 的在线可用性仍须在运行环境中检验。若 OpenRouter 当前模型或账户不支持 `web_fetch`，该能力会明确失败，不伪造页面证据。

## 10. v5.0 后续修补：天气地名与工具预算

2026-09-28 北京三日游 Run 暴露出 `weather_forecast` 的地理编码歧义：以完整输入直接查询时，“北京市”可能无结果；“北京”可能返回多个同名候选，旧选择器不看地点类型。修补后先规范化“市”后缀，拆出并核对国家／行政区，按城市输入语言发起地理编码；若存在唯一国家首都则选它，其他城市只在显著人口优势时自动选取。不能判定时仍明确失败，避免把错误地点的天气写入报告。

`RunPolicy` 及前端三个预设的默认工具预算改为每次 Agent 决策 5 次、全程 20 次。Planner 仍最多一次背景查询；失败请求仍占工具槽，所有并发研究任务共享全程预算。运行中的和历史 Run 不会自动继承新默认值；重跑或新建 Run 才会应用。遇到反复失败应先检查工具参数、供应商错误码和地点解析，而不只提高预算。

## 11. v5.0 后续修补：HTTP 网页证据与失败作用范围

web_fetch 当前由独立 WebFetchTool 实现，与 OpenRouter 搜索工具分别注册。它读取 HTTP 响应并提取原文，再返回 ToolResult／Evidence；model_calls_per_execution 为 0。旧章节中关于 OpenRouter fetch、annotations 和 PDF 支持的内容仅描述历史实现。

每次跳转都重新验证目的地址，实际连接使用已检查的 IP，Host／TLS SNI 保持原域名，证书验证始终启用。无 JavaScript 浏览器、登录会话或 PDF 解析能力；不可读页面应换用合适来源。来源记录保留 requested_url、final_url、redirect_chain、retrieved_at、content_kind=source_excerpt 和摘录范围。网页请求成功不等于成功标准已满足。

新增可选扩展点 BaseTool.failure_scope(arguments)：返回字符串表示不可重试失败所作用的目标，默认 None 表示不启用。web_fetch 返回规范化 URL，忽略 focus；这个钩子与成功结果去重的 cache_identical_calls 分开。ToolCallRecord.retryable 默认 None 兼容历史数据，新失败记录从 ToolResult.retryable 写入。运行器依据本阶段与 prior_calls 中明确不可重试的失败判断重复目标；重复请求会触发 suppressed=true 的 tool_failed 事件、停止当前工具阶段，不再次发出请求，也不占工具预算。必需能力尚未完成时仍保持任务失败，不能把跳过请求当成成功。并行且尚未合并记录的分支不保证共享这项失败抑制。

回归测试见 backend/tests/test_web_fetch.py，覆盖不含模型引用字段的 HTML、相对重定向、中文编码、正文摘录、内网／混合 DNS 拒绝、连接 IP 与 Host/SNI 一致性、重定向限制、HTTP 错误、流式大小限制、重复失败抑制和其他来源恢复。协议依据：[HTTPCore SNI 扩展](https://www.encode.io/httpcore/extensions/#sni_hostname)。

## 12. v5.0 后续修补：结构化导航地点

`map_route.py` 新增 `AmapPlace` 与 `PlaceInput`，由工具的 JSON Schema 自动暴露给模型。两点路线与多站行程共用此契约，不需要额外正则判断任务语义：

```json
{
  "city": "北京市",
  "origin": {"name": "王府井", "district": "东城区"},
  "destination": {"name": "中国国家博物馆", "district": "东城区"},
  "mode": "driving"
}
```

地点可选字段为 `district`、`address`、`entrance`，未知用 null 或省略；不接受模型自填坐标或 POI ID。保留旧字符串输入。`map_itinerary.stops` 同样支持结构化地点与字符串混合，仍为 2–6 站、返回逐段链接，不声称生成一条包含所有途经点的链接。`poi_details` 的参数契约本轮未改动。

解析流程：拼接已知约束检索 → 校验有效坐标 → 去除同 ID 且关键字段相同的重复项 → 严格核对区县／地址 → 唯一名称（包含指定入口）匹配。完整行政市名前缀和括号／连字符可规范化，但不抹掉门号、方向或附属设施名称。旧完整地址字符串仅在供应商地址精确匹配时接受。地址不做模糊包含判断，避免把 1 号误认为 11 号。接口限定 keywords 最长 80 字符，page_size 最大 25；本工具遵循该限制。参数与响应字段依据：[高德搜索 POI 2.0 官方文档](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch)。

歧义返回只含应用生成的安全提示、端点角色和路段编号，原始候选不回传模型或持久化。模型依据自己的原始请求和上游任务修正限定条件；不足以修正时保留缺口。当前不包含候选选择 UI、用户交互暂停或候选 LLM 排序，也不自动选第一个结果。

`ToolCallRecord.failure_scope` 为可选的失败作用域标识，运行器优先使用已存标识，旧记录仍尝试从安全参数计算。地图实现返回规范化请求的 SHA-256（包含出行模式与地点限定），使脱敏后的调用记录也能跨阶段识别同一失败请求；该指纹不替代成功结果缓存。网页工具仍使用其规范化 URL 作为作用域。未知重试语义的历史记录不追溯锁定，暂时性错误不锁定。仍使用 5／20 工具预算与已有失败事件展示。

## 13. v5.0 后续修补：组合能力、临时结果与拒绝预算

任务新增 `capability_alternatives: list[list[str]]`，默认 `[]` 兼容旧数据。`required_capabilities` 为 AND；每个 alternatives 内层数组为 OR，外层为 AND。每组 2–16 个不同能力，最多 8 组，不能与全部必需项重叠；目录校验拒绝未知能力。要求 fresh data 时必须存在必需的新数据能力，或存在所有分支均能获取新数据的可选组。每个可选组至少要有一个可用实现。扩展工具不需要修改能力 ID 枚举，Schema 继续由目录生成。

Runtime 保持完整的角色已授权可用目录，用 `tool_choice` 引导缺失能力；角色权限、配置可用性和执行前校验不取消。临时工具向后续模型只发送安全摘要和空 `data`，不发送候选或供应商原始字段，从而允许 POI→路线等组合任务继续；原始高德数据仍只在单次工具执行内存中使用。导航链接仅通过既有安全记录／成稿上下文输出。

新增 `BaseTool.validate_context(arguments, context)` 钩子，默认无附加约束；`ToolContext.user_query` 保留原始问题供工具做确定性安全检查。地图实现目前仅识别显式“从…方向门出发”入口格式，防止把同一地点降级为整体或地铁站，不声称解决所有自然语言地理约束。

未通过参数／上下文／可用目录校验的调用只形成拒绝事件，带 `executed=false` 和 `budget_counted=false`；最多 3 次拒绝后结束该工具阶段。它们不写入 ToolCallRecord，因此实际工具次数与拒绝事件次数不同。有效执行沿用原预算和历史记录计数；成功缓存复用仍沿用既有计数语义。`ToolBudgetExhausted` 是 RequiredToolError 的子类，供编排器停止无效任务重试。ToolBudget.remaining 检查不能替代原子 reserve，两个都保留，确保并行分支不突破 Run 上限。

回归见 `backend/tests/test_composite_tool_recovery.py`；运行说明和未完成的真实联调范围见 README 最新追加记录。

## 14. v5.0 后续修补：POI 预解析与正式名称

地图工具的地点处理顺序统一为：`PlaceInput` → 高德 `/v5/place/text` 候选 → 硬约束过滤 → `_poi_score()` 排序 → 高德正式 POI → 路径规划。`map_itinerary` 会先解析全部站点，再对相邻正式 POI 调用路径接口；站点解析缓存在单次工具调用的内存字典中，不跨调用保存。

硬约束包括区县／完整地址、有效坐标、入口方向及门号、目标类型。评分优先级为正式名称完全匹配、入口锚定匹配、完整地址精确匹配、限阈值名称相似；显式区县和地址加分。同分采用供应商响应顺序。实现使用 Python `SequenceMatcher` 计算局部文本相似度，该得分仅用于本次候选排序，不应显示为置信度、供应商评分或研究事实。

路线请求与高德 URI 使用选中结果的正式 `name`、`id`、`location`。这些字段和 `match_score` 属于临时数据，仍受地图输出脱敏策略约束；安全记录只允许导航 URL、调用状态和应用生成的摘要。新增地图扩展若复用该解析器，不得把内部候选或分数写入 Evidence、ToolCallRecord、Event、报告或 Trace。

入口匹配允许供应商在目标场所名与方向门之间加入校区／园区限定词，但不得改变场所锚点、方向或显式门号，也不得把场所入口降级为同名交通、停车、售票、餐饮或零售设施。没有有效候选时保持失败；自动最高分策略不是允许低相关候选兜底。

回归见 `backend/tests/test_poi_preflight.py` 和 `backend/tests/test_map_disambiguation.py`。
