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
