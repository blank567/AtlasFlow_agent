# Evaluation plan

## 测试层次

### 1. 离线单元与工作流测试

`backend/tests` 覆盖切块、混合检索元数据、工具 schema/权限、OpenRouter 请求契约、工作流
路由和 API。测试通过 `ProviderBundle` 注入仅供测试使用的 doubles，并用进程内 HTTP transport
模拟 provider 响应；因此运行 `pytest` 不需要 API key，也不会产生 OpenRouter 或 Web Search
费用。这种隔离只存在于测试，不是应用可选择的运行模式。

```powershell
conda activate langchain
python -m pytest
```

### 2. 真实 Provider 冒烟测试

`scripts/live_provider_smoke.py` 会读取 `.env`，实际调用 OpenRouter Chat、Embedding、Rerank
和 Web Search。它只输出布尔状态、向量维度、网页证据数量和错误，不输出密钥或完整内容。

```powershell
conda activate langchain
python scripts/live_provider_smoke.py
```

该命令会联网，可能消耗模型额度或产生 Web Search 费用；执行前应确认模型、区域、余额和密钥
权限。任何 provider 失败都会如实失败，没有 Mock fallback。

### 3. 在线回归集

`evals/run_local.py` 虽保留历史文件名，但现在使用真实 provider。必须显式传入 `--live`，以
避免误触发远程调用：

```powershell
conda activate langchain
python evals/run_local.py --live
```

当前 JSONL 数据集检查 required source IDs、expected tool names 和最终 run status。真实模型与
Web 内容可能变化，因此结果不应被误解为完全确定性的单元测试；失败样本应结合 LangSmith trace
和原始 evidence 分析。

### 4. LangSmith 评测（可选）

设置 `LANGSMITH_TRACING=true`、`LANGSMITH_API_KEY` 和 `LANGSMITH_PROJECT` 后，workflow、
Agent、LLM 与 tool span 会上传到项目。默认的 `LANGSMITH_TRACE_CONTENT=false` 会用占位信息
替代输入与输出正文；只有使用已脱敏的演示数据并确实需要调试内容时，才设置为 `true`。当前
仓库已接入 tracing，但 LangSmith Dataset evaluator 仍属于下一阶段：计划把线上失败脱敏后
沉淀为固定数据集，用于比较 prompt、模型和 RAG 版本。

## 指标

| 指标 | 计算方法 | v0.2 目标 |
|---|---|---|
| 工具调用覆盖 | expected vs actual tool names | ≥ 90% |
| 工具参数有效率 | schema-valid calls / all calls | ≥ 98% |
| Retrieval hit rate@5 | gold source 是否出现在前五条 | ≥ 85% |
| Citation coverage | 有引用的事实性结论 / 全部事实性结论 | ≥ 90% |
| Citation correctness | 引用确实支持结论 / 全部引用 | ≥ 85% |
| Completion rate | completed runs / started runs | ≥ 99% |
| P95 latency | trace 端到端耗时 | 按场景设基线 |
| Cost per successful run | provider usage / completed runs | 持续记录，不隐藏 |

“工具调用覆盖”是当前固定 Researcher 策略的回归指标，不等同于 LLM 自主选择工具的准确率。
若后续将工具选择交给模型，应另外建立 gold tool-selection 数据集。

## 回归工作流

1. 从失败 run 或 LangSmith trace 选择代表性样本；
2. 删除密钥、个人信息和机密文档内容；
3. 增加参考答案、必需来源、预期工具和失败类别；
4. 先运行离线测试，确认协议与路由没有回归；
5. 经人工确认后运行 `python evals/run_local.py --live`；
6. 比较总分、单样本结果、延迟和费用，再决定是否合并。

## 结果解释与数据安全

- Free 或动态路由模型的可用性、区域限制、速率限制和输出可能变化；记录实际模型与时间。
- 第一次知识库查询会为所有未向量化 chunk 调用 Embedding，延迟通常高于后续查询。
- Web Search 必须返回 URL citation 才计为成功；只有自然语言摘要而没有 URL 不能通过。
- 仅当 `LANGSMITH_TRACE_CONTENT=true` 时，查询、证据片段和模型输出才会进入 trace；启用前
  必须先脱敏评测数据。
- 使用第三方免费模型时不要提交敏感或保密数据，并遵守对应 provider 的数据政策。
