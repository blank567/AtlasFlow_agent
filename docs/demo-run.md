# Demo run

## 演示目标

面试时建议使用这一问题：

> AtlasFlow 如何通过工具调用和 RAG 提升多 Agent 结果可信度？

这条路径会同时展示 LangGraph 条件路由、真实 OpenRouter 模型、混合 RAG、Rerank、Web
Search URL citations、SSE 事件和可选 LangSmith trace。

## 演示前准备

1. 激活已有环境并安装项目依赖：

   ```powershell
   conda activate langchain
   python -m pip install -e ".[dev]"
   ```

2. 在仓库根目录 `.env` 配置 `LLM_*`、`EMBEDDING_*`、`RERANK_*` 和 `SEARCH_*`。
   所有 provider 都必须是 `openrouter`。`SEARCH_API_KEY` 可留空以复用 `LLM_API_KEY`，但
   模型名和其他真实调用所需的密钥必须有效。

3. 可选启用 LangSmith：填写 `LANGSMITH_API_KEY`，设置 `LANGSMITH_TRACING=true` 和
   `LANGSMITH_PROJECT=atlasflow-demo`。默认保持 `LANGSMITH_TRACE_CONTENT=false`，这样仍可
   展示 span 结构、状态和耗时，但不会上传正文。只有数据已脱敏且需要检查 prompt/output 时才
   将它设为 `true`；不演示 trace 时保持 tracing 为 `false`。

4. 建议先运行最小真实调用检查：

   ```powershell
   python scripts/live_provider_smoke.py
   ```

   该检查会真实联网并可能产生费用，尤其是 Web Search。失败时先处理认证、模型可用性、区域
   或配额问题；系统不会用假数据继续。

## 启动

后端使用 app factory：

```powershell
conda activate langchain
python -m uvicorn atlasflow.main:create_app --factory --app-dir backend/src --host 127.0.0.1 --port 8000 --reload
```

另开终端启动前端：

```powershell
cd frontend
npm install
npm run dev
```

打开前端显示的本地地址。也可以先通过 `/api/v1/health` 验证后端，再从 UI 发起 run。

## 预期流程

1. Supervisor 接收任务并初始化 run state。
2. Planner 调用真实 OpenRouter LLM，按 JSON Schema 生成研究计划；步骤数由模型决定，不应
   在演示词中硬编码。
3. Researcher 通过 ToolRegistry 调用 `knowledge_search`。首次检索会为尚未向量化的 chunk
   调用真实 Embedding API，然后执行 BM25、向量余弦相似度、RRF 和真实 Rerank。
4. Researcher 调用 `web_search`，OpenRouter server tool 返回 URL citation annotations；
   没有 URL citation 时该工具会明确失败。
5. Writer 基于计划与 evidence 生成带 `[S1]` 风格引用的 Markdown 草稿。
6. Critic 调用真实 LLM 检查证据与引用。若发现问题且仍有预算，图会回到 Researcher，并将
   critique 加入新一轮检索；否则进入 Reporter。
7. Reporter 输出最终报告；若达到预算仍有问题，会附上“质量检查备注”。
8. 浏览器通过 SSE 持续显示 Agent、工具调用、成功/失败和最终状态。
9. 如果开启 LangSmith，可在项目中查看 workflow、Agent、LLM 和 tool 的嵌套 span。

真实模型和网页内容具有波动性，因此计划步骤、引用数量和是否触发第二轮可能每次不同。

## 面试时重点展示

- 展开一条知识库 evidence，说明 lexical、vector、fusion、rerank 四阶段分数如何形成；
- 展开一条网页 evidence，验证 URI 来自 OpenRouter 的 URL citation，而不是模型编造链接；
- 展示 ToolRegistry 的 schema、low-risk 授权、超时和重试边界；
- 展示 Critic 条件边和最大迭代次数，解释为什么不会无限循环；
- 若启用 LangSmith，沿父子 span 定位一次慢调用或失败 provider；
- 说明当前持久化仍是内存实现，进程重启后 run、上传文档和向量会丢失。

## 常见失败

| 现象 | 检查项 |
|---|---|
| 启动即报 API key/model 配置错误 | `.env` 中四类 provider、key 与 model slug |
| 401/403 | 密钥权限、模型区域可用性和 provider 账户状态 |
| 429/5xx | 速率限制或 provider 故障；客户端只做有限重试 |
| Web Search 标记失败 | 模型是否支持 server tool、响应是否含 URL citation |
| RAG 报维度或索引错误 | Embedding/Rerank 模型响应是否与配置匹配 |
| LangSmith 没有 trace | `LANGSMITH_TRACING`、key、endpoint、project 与网络 |
| Trace 有 span 但没有正文 | 这是 `LANGSMITH_TRACE_CONTENT=false` 的预期脱敏行为 |

不要把 provider 失败包装成成功演示：真实失败、结构化错误和可追踪诊断本身就是该项目的工程
亮点。
