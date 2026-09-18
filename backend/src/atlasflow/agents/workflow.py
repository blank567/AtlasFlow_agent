from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from atlasflow.agents.gateway import ModelGateway
from atlasflow.observability import traced
from atlasflow.schemas import Evidence, RunEvent, ToolCallRecord
from atlasflow.tools import RiskLevel, ToolContext, ToolRegistry


class AgentState(TypedDict, total=False):
    run_id: str
    query: str
    plan: list[str]
    evidence: list[Evidence]
    tool_calls: list[ToolCallRecord]
    draft: str
    critiques: list[str]
    iteration: int
    needs_revision: bool
    report: str


EventSink = Callable[[str, RunEvent], Awaitable[None]]


class ResearchWorkflow:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        model: ModelGateway,
        event_sink: EventSink,
        top_k: int = 5,
        max_iterations: int = 2,
    ) -> None:
        self.registry = registry
        self.model = model
        self.event_sink = event_sink
        self.top_k = top_k
        self.max_iterations = max_iterations
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("supervisor", self._supervisor)
        graph.add_node("planner", self._planner)
        graph.add_node("researcher", self._researcher)
        graph.add_node("writer", self._writer)
        graph.add_node("critic", self._critic)
        graph.add_node("reporter", self._reporter)

        graph.add_edge(START, "supervisor")
        graph.add_edge("supervisor", "planner")
        graph.add_edge("planner", "researcher")
        graph.add_edge("researcher", "writer")
        graph.add_edge("writer", "critic")
        graph.add_conditional_edges(
            "critic",
            self._after_critic,
            {"revise": "researcher", "finish": "reporter"},
        )
        graph.add_edge("reporter", END)
        return graph.compile()

    async def _emit(self, state: AgentState, agent: str, message: str, **data: Any) -> None:
        await self.event_sink(
            state["run_id"], RunEvent(type="agent", agent=agent, message=message, data=data)
        )

    @traced(name="agent.supervisor")
    async def _supervisor(self, state: AgentState) -> dict[str, Any]:
        await self._emit(state, "supervisor", "任务已接收，开始协调 Agent")
        return {"iteration": 0, "evidence": [], "tool_calls": [], "critiques": []}

    @traced(name="agent.planner")
    async def _planner(self, state: AgentState) -> dict[str, Any]:
        plan = await self.model.create_plan(state["query"])
        await self._emit(state, "planner", "已生成研究计划", steps=len(plan))
        return {"plan": plan}

    @traced(name="agent.researcher")
    async def _researcher(self, state: AgentState) -> dict[str, Any]:
        context = ToolContext(
            run_id=state["run_id"],
            agent_name="researcher",
            approved_risks={RiskLevel.LOW},
        )
        evidence = list(state.get("evidence", []))
        calls = list(state.get("tool_calls", []))

        query = state["query"]
        if state.get("critiques"):
            query = f"{query} {' '.join(state['critiques'])}"
        # A valid API query can reach the schema limit; revision feedback must not push the
        # downstream tool arguments beyond that same contract.
        query = query[:4000]

        tool_requests: list[tuple[str, dict[str, Any]]] = [
            ("knowledge_search", {"query": query, "top_k": self.top_k})
        ]
        if self.registry.contains("web_search"):
            tool_requests.append(("web_search", {"query": query, "max_results": 3}))

        for tool_name, arguments in tool_requests:
            result = await self.registry.execute(tool_name, arguments, context)
            evidence.extend(result.evidence)
            calls.append(
                ToolCallRecord(
                    tool_name=tool_name,
                    arguments=arguments,
                    success=result.success,
                    duration_ms=result.duration_ms,
                    evidence_ids=[item.id for item in result.evidence],
                    error=result.error,
                )
            )
            await self._emit(
                state,
                "researcher",
                f"工具 {tool_name} 调用完成",
                success=result.success,
                evidence_count=len(result.evidence),
            )

        unique = {item.source_id: item for item in evidence}
        return {"evidence": list(unique.values()), "tool_calls": calls}

    @traced(name="agent.writer")
    async def _writer(self, state: AgentState) -> dict[str, Any]:
        draft = await self.model.write_draft(
            state["query"], state.get("plan", []), state.get("evidence", [])
        )
        await self._emit(state, "writer", "已生成报告草稿")
        return {"draft": draft}

    @traced(name="agent.critic")
    async def _critic(self, state: AgentState) -> dict[str, Any]:
        critiques = await self.model.critique(
            state["query"], state.get("draft", ""), state.get("evidence", [])
        )
        iteration = state.get("iteration", 0) + 1
        needs_revision = bool(critiques) and iteration < self.max_iterations
        await self._emit(
            state,
            "critic",
            "质量检查完成",
            issues=len(critiques),
            needs_revision=needs_revision,
        )
        return {
            "critiques": critiques,
            "iteration": iteration,
            "needs_revision": needs_revision,
        }

    @staticmethod
    def _after_critic(state: AgentState) -> str:
        return "revise" if state.get("needs_revision", False) else "finish"

    @traced(name="agent.reporter")
    async def _reporter(self, state: AgentState) -> dict[str, Any]:
        report = state.get("draft", "")
        if state.get("critiques"):
            report += "\n\n## 质量检查备注\n\n" + "\n".join(
                f"- {issue}" for issue in state["critiques"]
            )
        await self._emit(state, "reporter", "最终报告已生成")
        return {"report": report}

    @traced(name="workflow.research-run")
    async def run(self, *, run_id: str, query: str) -> AgentState:
        return await self.graph.ainvoke({"run_id": run_id, "query": query})
