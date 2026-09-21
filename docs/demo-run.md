# Demo run

## 演示目标

v0.2 的面试演示重点是“多 Agent 如何被可靠地编排”，不是 Tool 或 RAG 的数量。推荐问题：

> 评估一个多 Agent 研究系统的架构、主要风险与分阶段改进方案。

它能展示结构化计划、DAG 并行、Critic 路由、QualityGate、失败预算、SSE 审计和可选人工审批。

## 演示前准备

1. 使用已有环境安装项目：

   ```powershell
   conda activate langchain
   python -m pip install -e ".[dev]"
   ```

2. 在根目录 `.env` 填写真实 `LLM_*` 配置。运行时没有 Mock 或离线回退。Embedding、Rerank
   与 Search 配置仍供保留的 RAG/Tool API 使用，但当前 Agent 主图只调用 LLM。

3. 可选启用 LangSmith：填写 `LANGSMITH_API_KEY`，设置 `LANGSMITH_TRACING=true` 和
   `LANGSMITH_PROJECT=atlasflow-demo`。默认保持 `LANGSMITH_TRACE_CONTENT=false`，只展示 span
   结构、状态和耗时。

4. 真实联网前先执行离线测试，再按需执行冒烟：

   ```powershell
   pytest
   python scripts/live_workflow_smoke.py
   ```

   第二条命令会真实联网并可能产生费用。

## 启动

```powershell
conda activate langchain
python -m uvicorn atlasflow.main:create_app --factory --app-dir backend/src --host 127.0.0.1 --port 8000 --reload
```

另开终端：

```powershell
Set-Location frontend
npm run dev
```

## 自动批准路径

1. 保持“自动批准”勾选并提交问题。
2. Planner 返回 2–5 个结构化任务；在“任务 DAG”区域解释任务 ID、目标、成功标准和依赖。
3. 无依赖的任务被同一波调度；SSE 中可看到多个 `task_scheduled` 与 Researcher 事件。
4. Researcher 结果 fan-in 后，Critic 选择 `accept`、`supplement` 或 `replan`。
5. Synthesizer 生成报告，QualityGate 给出分数并选择 `accept`、`revise` 或 `replan`。
6. 最后一个审计事件是 `run_completed` 或 `run_degraded`，随后 SSE 只发送一次 `done`。

真实模型的计划和路由可能变化，但循环预算是固定的：单任务最多 2 次尝试、补充研究最多 1 轮、
全局重规划最多 1 次、报告修订最多 2 次。

## Human-in-the-loop 路径

1. 取消“自动批准”并提交任务。
2. Run 进入 `waiting_approval`，LangGraph 已通过内存 checkpointer 暂停。
3. 展示三种操作：

   - 批准：继续原 DAG；
   - 编辑：修改 JSON 后提交，系统重新检查 ID、依赖和环；
   - 取消：Run 进入 `cancelled`，不生成报告。

当前恢复只保证同一进程内有效；进程重启后的恢复将在数据库与持久 checkpointer 阶段完成。

## 建议讲解顺序

1. 先讲 `contracts.py`：所有 Agent 边界都有明确、可验证的数据模型。
2. 再讲 `gateway.py`：控制输出使用严格 JSON Schema，报告使用纯文本，全部接入 LangSmith。
3. 展示 `workflow.py`：确定性 Supervisor、`Send` 动态并行、Quorum、Critic 和 QualityGate。
4. 展示 `service.py`：状态机、原子终态事件和后台执行。
5. 展示 UI 与 SSE：计划、任务状态、决策、质量分和降级原因都能被观察。
6. 最后运行离线分支测试，说明并发上限和失败分支不是口头设计。

## Tool/RAG 的当前边界

`/tools`、`/documents`、HybridRetriever、Web Search 和 Calculator 仍保留，可单独演示接口与
实现；v0.2 主图不会自动调用它们。面试时应明确说明这是分阶段重构：先稳定 Agent 协议，下一版
再通过 Context Provider 接回工具选择、风险审批、证据引用与 RAG 质量评测。

## 常见失败

| 现象 | 检查项 |
|---|---|
| 启动即报 API key/model 配置错误 | `.env` 中真实 provider、key 与 model slug |
| Planner/决策输出契约失败 | 模型是否支持严格 JSON Schema；网关只做一次格式重试 |
| Run 为 `completed_with_warnings` | 展开 warnings、失败任务、Critic 和 QualityGate 决策 |
| Run 为 `failed` | 检查 Quorum、核心 Agent 错误和最后一个 route |
| 一直 `waiting_approval` | 在 UI 或审批 API 提交 approve/edit/cancel |
| LangSmith 没有 trace | tracing、key、endpoint、project 与网络配置 |

真实 provider 失败本身也是可观测性演示的一部分，不应包装成伪成功。
