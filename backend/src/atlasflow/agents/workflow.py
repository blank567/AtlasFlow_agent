from __future__ import annotations

import asyncio
import operator
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from atlasflow.agents.contracts import (
    AgentError,
    CritiqueDecision,
    CritiqueRoute,
    DraftVersion,
    ExecutionMetrics,
    PlanLineage,
    QualityDecision,
    QualityRoute,
    ResearchPlan,
    ResearchResult,
    ResearchTask,
    ReviewContext,
    RouteRecord,
    RunPolicy,
)
from atlasflow.agents.gateway import ModelGateway
from atlasflow.observability import TraceSegmentSink, trace_segment
from atlasflow.schemas import ApprovalAction, RunEvent, RunEventType, RunStatus

EventSink = Callable[[str, RunEvent], Awaitable[None]]

MAX_INITIAL_TASKS = 5
MAX_PLAN_TASKS = 7
MAX_RESEARCH_ATTEMPTS = 2
MAX_SUPPLEMENT_ROUNDS = 1
MAX_REPLANS = 1
MAX_REVISIONS = 2
QUORUM = 0.60
QUALITY_THRESHOLD = 80
DEFAULT_RECURSION_LIMIT = 96

_CHECKPOINT_TYPES = [
    ("atlasflow.agents.contracts", name)
    for name in (
        "AgentError",
        "CritiqueDecision",
        "CritiqueRoute",
        "DraftVersion",
        "ExecutionMetrics",
        "PlanLineage",
        "QualityDecision",
        "QualityRoute",
        "ReplanReason",
        "ResearchPlan",
        "ResearchResult",
        "ResearchTask",
        "ReviewContext",
        "ReviewIssue",
        "RouteRecord",
        "RunPolicy",
        "Severity",
        "SupplementBatch",
    )
]


def _merge_results(
    current: list[ResearchResult], incoming: list[ResearchResult]
) -> list[ResearchResult]:
    """Merge parallel output, with plan version as part of the identity."""

    merged = {(item.plan_version, item.task_id): item for item in current}
    for item in incoming:
        merged[(item.plan_version, item.task_id)] = item
    return list(merged.values())


def _merge_warnings(current: list[str], incoming: list[str]) -> list[str]:
    return list(dict.fromkeys([*current, *incoming]))


class AgentState(TypedDict, total=False):
    run_id: str
    query: str
    auto_approve: bool
    policy: RunPolicy
    started_at: datetime
    plan: ResearchPlan
    plans: list[ResearchPlan]
    plan_lineage: PlanLineage
    plan_lineages: list[PlanLineage]
    research_results: Annotated[list[ResearchResult], _merge_results]
    critique_history: Annotated[list[CritiqueDecision], operator.add]
    review_contexts: Annotated[list[ReviewContext], operator.add]
    draft_versions: Annotated[list[DraftVersion], operator.add]
    quality_history: Annotated[list[QualityDecision], operator.add]
    route_history: Annotated[list[RouteRecord], operator.add]
    errors: Annotated[list[AgentError], operator.add]
    warnings: Annotated[list[str], _merge_warnings]
    model_calls: Annotated[int, operator.add]
    supplement_rounds: int
    replan_count: int
    revision_count: int
    next_plan_version: int
    ready_tasks: list[ResearchTask]
    task: ResearchTask
    dependency_results: list[ResearchResult]
    synthesis_mode: Literal["fresh", "revise"]
    requested_final_status: str | None
    final_status: str | None
    fatal_error: str | None
    report: str | None
    metrics: ExecutionMetrics


@dataclass(frozen=True, slots=True)
class WorkflowExecution:
    """Stable boundary returned to the run service."""

    state: dict[str, Any]
    paused: bool = False
    final: bool = False

    @property
    def status(self) -> str | None:
        value = self.state.get("final_status")
        return str(value) if value is not None else None


class ResearchWorkflow:
    """Budgeted v0.2 orchestration; Researchers use only the model gateway."""

    def __init__(
        self,
        *,
        model: ModelGateway,
        event_sink: EventSink,
        checkpointer: MemorySaver | None = None,
        max_concurrency: int = 3,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
        max_initial_tasks: int = MAX_INITIAL_TASKS,
        max_research_attempts: int = MAX_RESEARCH_ATTEMPTS,
        quorum_ratio: float = QUORUM,
        max_supplement_rounds: int = MAX_SUPPLEMENT_ROUNDS,
        max_supplement_tasks: int = 2,
        max_tasks_per_plan: int = MAX_PLAN_TASKS,
        max_replans: int = MAX_REPLANS,
        max_revisions: int = MAX_REVISIONS,
        quality_threshold: int = QUALITY_THRESHOLD,
        trace_segment_sink: TraceSegmentSink | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= 3:
            raise ValueError("max_concurrency must be between 1 and 3")
        if recursion_limit < 32:
            raise ValueError("recursion_limit is too small for the bounded workflow")
        if not 2 <= max_initial_tasks <= 5:
            raise ValueError("max_initial_tasks must be between 2 and 5")
        if not 1 <= max_research_attempts <= 2:
            raise ValueError("max_research_attempts must be between 1 and 2")
        if not 0 < quorum_ratio <= 1:
            raise ValueError("quorum_ratio must be in (0, 1]")
        if not 0 <= max_supplement_rounds <= 1:
            raise ValueError("max_supplement_rounds must be 0 or 1")
        if not 1 <= max_supplement_tasks <= 2:
            raise ValueError("max_supplement_tasks must be between 1 and 2")
        if not max_initial_tasks <= max_tasks_per_plan <= 7:
            raise ValueError(
                "max_tasks_per_plan must be between max_initial_tasks and 7"
            )
        if not 0 <= max_replans <= 1:
            raise ValueError("max_replans must be 0 or 1")
        if not 0 <= max_revisions <= 2:
            raise ValueError("max_revisions must be between 0 and 2")
        if not 80 <= quality_threshold <= 100:
            raise ValueError("quality_threshold must be between 80 and 100")
        self.model = model
        self.event_sink = event_sink
        self.max_concurrency = max_concurrency
        self.recursion_limit = recursion_limit
        self.max_initial_tasks = max_initial_tasks
        self.max_research_attempts = max_research_attempts
        self.quorum_ratio = quorum_ratio
        self.max_supplement_rounds = max_supplement_rounds
        self.max_supplement_tasks = max_supplement_tasks
        self.max_tasks_per_plan = max_tasks_per_plan
        self.max_replans = max_replans
        self.max_revisions = max_revisions
        self.quality_threshold = quality_threshold
        self.trace_segment_sink = trace_segment_sink
        self.checkpointer = checkpointer or MemorySaver(
            serde=JsonPlusSerializer(allowed_msgpack_modules=_CHECKPOINT_TYPES)
        )
        self._research_slots = asyncio.Semaphore(max_concurrency)
        self._concurrency_lock = asyncio.Lock()
        self._active_researchers: dict[str, int] = {}
        self._peak_researchers: dict[str, int] = {}
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("supervisor", self._supervisor)
        graph.add_node("planner", self._planner)
        graph.add_node("approval", self._approval)
        graph.add_node("schedule_wave", self._schedule_wave)
        graph.add_node("researcher", self._researcher)
        graph.add_node("wave_join", self._wave_join)
        graph.add_node("research_gate", self._research_gate)
        graph.add_node("critic", self._critic)
        graph.add_node("synthesizer", self._synthesizer)
        graph.add_node("quality_gate", self._quality_gate)
        graph.add_node("finalizer", self._finalizer)

        graph.add_edge(START, "supervisor")
        graph.add_edge("supervisor", "planner")
        graph.add_conditional_edges(
            "planner",
            self._after_planner,
            {"approval": "approval", "finalizer": "finalizer"},
        )
        graph.add_conditional_edges(
            "approval",
            self._after_approval,
            {"schedule_wave": "schedule_wave", "finalizer": "finalizer"},
        )
        graph.add_conditional_edges(
            "schedule_wave", self._dispatch_wave, ["researcher", "research_gate"]
        )
        graph.add_edge("researcher", "wave_join")
        graph.add_edge("wave_join", "schedule_wave")
        graph.add_conditional_edges(
            "research_gate",
            self._after_research_gate,
            {"planner": "planner", "critic": "critic", "finalizer": "finalizer"},
        )
        graph.add_conditional_edges(
            "critic",
            self._after_critic,
            {
                "planner": "planner",
                "schedule_wave": "schedule_wave",
                "synthesizer": "synthesizer",
                "finalizer": "finalizer",
            },
        )
        graph.add_conditional_edges(
            "synthesizer",
            self._after_synthesizer,
            {"quality_gate": "quality_gate", "finalizer": "finalizer"},
        )
        graph.add_conditional_edges(
            "quality_gate",
            self._after_quality_gate,
            {
                "planner": "planner",
                "synthesizer": "synthesizer",
                "finalizer": "finalizer",
            },
        )
        graph.add_edge("finalizer", END)
        return graph.compile(checkpointer=self.checkpointer)

    def _config(self, run_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": run_id},
            "recursion_limit": self.recursion_limit,
            "max_concurrency": self.max_concurrency,
        }

    async def start(
        self,
        *,
        run_id: str,
        query: str,
        auto_approve: bool = True,
        policy: RunPolicy | None = None,
    ) -> WorkflowExecution:
        resolved_policy = policy or RunPolicy()
        if (
            resolved_policy.initial_task_count is not None
            and resolved_policy.initial_task_count > self.max_initial_tasks
        ):
            raise ValueError(
                "policy initial_task_count exceeds the configured initial-task budget"
            )
        if resolved_policy.required_supplement_rounds > self.max_supplement_rounds:
            raise ValueError(
                "policy required_supplement_rounds exceeds the configured supplement budget"
            )
        if (
            resolved_policy.supplement_task_count is not None
            and resolved_policy.supplement_task_count > self.max_supplement_tasks
        ):
            raise ValueError(
                "policy supplement_task_count exceeds the configured supplement-task budget"
            )
        initial: AgentState = {
            "run_id": run_id,
            "query": query,
            "auto_approve": auto_approve,
            "policy": resolved_policy,
            "started_at": datetime.now(UTC),
            "plans": [],
            "plan_lineages": [],
            "research_results": [],
            "critique_history": [],
            "review_contexts": [],
            "draft_versions": [],
            "quality_history": [],
            "route_history": [],
            "errors": [],
            "warnings": [],
            "model_calls": 0,
            "supplement_rounds": 0,
            "replan_count": 0,
            "revision_count": 0,
            "next_plan_version": 1,
            "ready_tasks": [],
            "synthesis_mode": "fresh",
            "requested_final_status": None,
            "final_status": None,
            "fatal_error": None,
            "report": None,
        }
        return await self._invoke(initial, run_id=run_id)

    async def run(
        self,
        *,
        run_id: str,
        query: str,
        auto_approve: bool = True,
        policy: RunPolicy | None = None,
    ) -> WorkflowExecution:
        return await self.start(
            run_id=run_id,
            query=query,
            auto_approve=auto_approve,
            policy=policy,
        )

    async def resume(
        self,
        *,
        run_id: str,
        action: ApprovalAction | str,
        edited_plan: ResearchPlan | Mapping[str, Any] | None = None,
    ) -> WorkflowExecution:
        action_value = (
            action.value if isinstance(action, ApprovalAction) else str(action)
        )
        if action_value not in {item.value for item in ApprovalAction}:
            raise ValueError(f"unsupported approval action: {action_value}")
        if action_value == ApprovalAction.EDIT.value and edited_plan is None:
            raise ValueError("edited_plan is required for action='edit'")
        if action_value != ApprovalAction.EDIT.value and edited_plan is not None:
            raise ValueError("edited_plan is only valid for action='edit'")
        snapshot = await self.graph.aget_state(self._config(run_id))
        if not snapshot.values:
            raise KeyError(f"workflow run not found: {run_id}")
        if not snapshot.interrupts:
            raise RuntimeError(f"workflow run is not waiting for approval: {run_id}")
        serialized: dict[str, Any] | None = None
        if edited_plan is not None:
            plan = (
                edited_plan
                if isinstance(edited_plan, ResearchPlan)
                else ResearchPlan.model_validate(edited_plan)
            )
            serialized = plan.model_dump(mode="json")
        return await self._invoke(
            Command(resume={"action": action_value, "edited_plan": serialized}),
            run_id=run_id,
        )

    async def _invoke(self, graph_input: Any, *, run_id: str) -> WorkflowExecution:
        config = self._config(run_id)
        segment_kind = "approval_resume" if isinstance(graph_input, Command) else "initial"
        async with trace_segment(
            atlasflow_run_id=run_id,
            name=f"atlasflow.workflow.{segment_kind}",
            kind=segment_kind,
            metadata={"workflow": "research", "entrypoint": segment_kind},
            sink=self.trace_segment_sink,
        ) as segment:
            await self.graph.ainvoke(graph_input, config=config)
            snapshot = await self.graph.aget_state(config)
            state = dict(snapshot.values)
            paused = bool(snapshot.interrupts)
            terminal = {
                RunStatus.COMPLETED.value,
                RunStatus.COMPLETED_WITH_WARNINGS.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            }
            final = state.get("final_status") in terminal
            if final:
                self._active_researchers.pop(run_id, None)
                self._peak_researchers.pop(run_id, None)
            segment.set_outputs(
                {
                    "paused": paused,
                    "final": final,
                    "status": state.get("final_status") or RunStatus.RUNNING.value,
                }
            )
            return WorkflowExecution(state=state, paused=paused, final=final)

    async def _emit(
        self,
        state: Mapping[str, Any],
        event_type: RunEventType,
        message: str,
        *,
        agent: str | None = None,
        node: str | None = None,
        task_id: str | None = None,
        plan_version: int | None = None,
        attempt: int | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        decision_reason: str | None = None,
        **data: Any,
    ) -> None:
        run_id = str(state["run_id"])
        await self.event_sink(
            run_id,
            RunEvent(
                run_id=run_id,
                event_type=event_type,
                message=message,
                agent=agent,
                node=node,
                task_id=task_id,
                plan_version=plan_version,
                attempt=attempt,
                status=status,
                duration_ms=duration_ms,
                decision_reason=decision_reason,
                data=data,
            ),
        )

    async def _record_route(
        self,
        state: Mapping[str, Any],
        *,
        from_node: str,
        to_node: str,
        reason: str,
        decision: str | None = None,
        plan_version: int | None = None,
        attempt: int = 1,
    ) -> RouteRecord:
        version = plan_version or self._plan_version(state)
        route = RouteRecord(
            from_node=from_node,
            to_node=to_node,
            reason=reason,
            decision=decision,
            plan_version=version,
            attempt=attempt,
        )
        await self._emit(
            state,
            RunEventType.ROUTE_SELECTED,
            f"路由：{from_node} → {to_node}",
            agent="supervisor",
            node=from_node,
            plan_version=version,
            attempt=attempt,
            decision_reason=reason,
            decision=decision,
            to_node=to_node,
        )
        return route

    @staticmethod
    def _plan_version(state: Mapping[str, Any]) -> int:
        plan = state.get("plan")
        if isinstance(plan, ResearchPlan):
            return plan.plan_version
        task = state.get("task")
        if isinstance(task, ResearchTask):
            return task.plan_version
        return max(int(state.get("next_plan_version", 1)), 1)

    @staticmethod
    def _current_results(state: Mapping[str, Any]) -> list[ResearchResult]:
        version = ResearchWorkflow._plan_version(state)
        return [
            item
            for item in state.get("research_results", [])
            if item.plan_version == version
        ]

    @staticmethod
    def _replace_plan(
        plans: list[ResearchPlan], plan: ResearchPlan
    ) -> list[ResearchPlan]:
        updated = [item for item in plans if item.plan_version != plan.plan_version]
        updated.append(plan)
        return sorted(updated, key=lambda item: item.plan_version)

    @staticmethod
    def _replace_lineage(
        lineages: list[PlanLineage], lineage: PlanLineage
    ) -> list[PlanLineage]:
        updated = [
            item for item in lineages if item.plan_version != lineage.plan_version
        ]
        updated.append(lineage)
        return sorted(updated, key=lambda item: item.plan_version)

    def _review_context(self, state: AgentState) -> ReviewContext:
        plan = state["plan"]
        lineage = state.get("plan_lineage")
        if not isinstance(lineage, PlanLineage):
            lineage = PlanLineage(plan_version=plan.plan_version, base_plan=plan)
        results = self._current_results(state)
        completed = {item.task_id for item in results}
        failed = {
            error.task_id
            for error in state.get("errors", [])
            if error.plan_version == plan.plan_version
            and error.code == "research_failed"
            and error.task_id is not None
            and error.task_id not in completed
        }
        task_order = list(plan.task_ids)
        used = int(state.get("supplement_rounds", 0))
        return ReviewContext(
            review_round=len(state.get("critique_history", [])) + 1,
            plan_version=plan.plan_version,
            plan_revision=lineage.revision,
            initial_task_ids=list(lineage.initial_task_ids),
            supplemental_task_ids=list(lineage.supplemental_task_ids),
            completed_task_ids=[item for item in task_order if item in completed],
            failed_task_ids=[item for item in task_order if item in failed],
            supplement_rounds_used=used,
            supplement_rounds_remaining=max(self.max_supplement_rounds - used, 0),
            current_task_count=len(plan.tasks),
            expected_task_count=(
                len(lineage.initial_task_ids) + len(lineage.supplemental_task_ids)
            ),
            policy=state.get("policy", RunPolicy()),
        )

    @staticmethod
    def _agent_error(
        *,
        agent: str,
        node: str,
        code: str,
        exc: BaseException,
        retryable: bool = False,
        attempt: int = 1,
        task_id: str | None = None,
        plan_version: int | None = None,
    ) -> AgentError:
        return AgentError(
            agent=agent,
            node=node,
            code=code,
            message=str(exc).strip() or type(exc).__name__,
            retryable=retryable,
            attempt=attempt,
            task_id=task_id,
            plan_version=plan_version,
        )

    async def _fatal_update(
        self,
        state: AgentState,
        *,
        agent: str,
        node: str,
        exc: BaseException,
        started: float,
    ) -> dict[str, Any]:
        error = self._agent_error(
            agent=agent,
            node=node,
            code=f"{node}_failed",
            exc=exc,
            plan_version=self._plan_version(state),
        )
        await self._emit(
            state,
            RunEventType.NODE_FAILED,
            f"{agent} 执行失败",
            agent=agent,
            node=node,
            plan_version=self._plan_version(state),
            status="failed",
            duration_ms=int((perf_counter() - started) * 1000),
            error=error.message,
        )
        route = await self._record_route(
            state,
            from_node=node,
            to_node="finalizer",
            reason=error.message,
            decision="failed",
        )
        return {
            "errors": [error],
            "fatal_error": error.message,
            "requested_final_status": RunStatus.FAILED.value,
            "route_history": [route],
        }

    async def _supervisor(self, state: AgentState) -> dict[str, Any]:
        started = perf_counter()
        await self._emit(
            state,
            RunEventType.NODE_STARTED,
            "任务已接收，Supervisor 开始编排",
            agent="supervisor",
            node="supervisor",
            plan_version=1,
            status=RunStatus.RUNNING.value,
        )
        route = await self._record_route(
            state,
            from_node="supervisor",
            to_node="planner",
            reason="创建首版研究计划",
            plan_version=1,
        )
        await self._emit(
            state,
            RunEventType.NODE_SUCCEEDED,
            "Supervisor 初始化完成",
            agent="supervisor",
            node="supervisor",
            plan_version=1,
            status="succeeded",
            duration_ms=int((perf_counter() - started) * 1000),
        )
        return {"route_history": [route]}

    async def _planner(self, state: AgentState) -> dict[str, Any]:
        started = perf_counter()
        version = int(state.get("next_plan_version", 1))
        await self._emit(
            state,
            RunEventType.NODE_STARTED,
            f"Planner 正在创建第 {version} 版计划",
            agent="planner",
            node="planner",
            plan_version=version,
            status="running",
        )
        try:
            plan = ResearchPlan.model_validate(
                await self.model.create_plan(
                    state["query"],
                    plan_version=version,
                    policy=state.get("policy", RunPolicy()),
                )
            )
            if plan.plan_version != version:
                raise ValueError(
                    f"Planner returned plan version {plan.plan_version}; expected {version}"
                )
            if not 2 <= len(plan.tasks) <= self.max_initial_tasks:
                raise ValueError(
                    f"Planner must create between 2 and {self.max_initial_tasks} initial tasks"
                )
            policy = state.get("policy", RunPolicy())
            if (
                version == 1
                and policy.initial_task_count is not None
                and len(plan.tasks) != policy.initial_task_count
            ):
                raise ValueError(
                    "Planner must create exactly "
                    f"{policy.initial_task_count} tasks for the initial plan"
                )
            lineage = PlanLineage(
                plan_version=plan.plan_version,
                base_plan=plan,
            )
            route = await self._record_route(
                {**state, "plan": plan},
                from_node="planner",
                to_node="approval",
                reason="计划已通过 DAG 校验",
                plan_version=version,
            )
            await self._emit(
                state,
                RunEventType.PLAN_CREATED,
                f"已生成第 {version} 版研究计划",
                agent="planner",
                node="planner",
                plan_version=version,
                status="succeeded",
                rationale=plan.rationale,
                plan=plan.model_dump(mode="json"),
                tasks=[task.model_dump(mode="json") for task in plan.tasks],
            )
            final_status: str | None = None
            if not state["auto_approve"]:
                final_status = RunStatus.WAITING_APPROVAL.value
                await self._emit(
                    state,
                    RunEventType.APPROVAL_REQUIRED,
                    "研究计划等待人工审批",
                    agent="planner",
                    node="approval",
                    plan_version=version,
                    status=final_status,
                    plan=plan.model_dump(mode="json"),
                )
            await self._emit(
                state,
                RunEventType.NODE_SUCCEEDED,
                "Planner 执行完成",
                agent="planner",
                node="planner",
                plan_version=version,
                status="succeeded",
                duration_ms=int((perf_counter() - started) * 1000),
            )
            return {
                "plan": plan,
                "plans": self._replace_plan(list(state.get("plans", [])), plan),
                "plan_lineage": lineage,
                "plan_lineages": self._replace_lineage(
                    list(state.get("plan_lineages", [])), lineage
                ),
                "ready_tasks": [],
                "synthesis_mode": "fresh",
                "final_status": final_status,
                "requested_final_status": None,
                "fatal_error": None,
                "route_history": [route],
                "model_calls": 1,
            }
        except Exception as exc:  # noqa: BLE001
            update = await self._fatal_update(
                state, agent="planner", node="planner", exc=exc, started=started
            )
            update["model_calls"] = 1
            return update

    @staticmethod
    def _after_planner(state: AgentState) -> str:
        return "finalizer" if state.get("fatal_error") else "approval"

    async def _approval(self, state: AgentState) -> dict[str, Any]:
        plan = state["plan"]
        if state["auto_approve"]:
            action_value, edited_payload = ApprovalAction.APPROVE.value, None
        else:
            response = interrupt(
                {
                    "run_id": state["run_id"],
                    "plan": plan.model_dump(mode="json"),
                    "actions": [item.value for item in ApprovalAction],
                }
            )
            if not isinstance(response, Mapping):
                raise ValueError("approval response must be an object")
            action_value = str(response.get("action", ""))
            edited_payload = response.get("edited_plan")
        if action_value not in {item.value for item in ApprovalAction}:
            raise ValueError(f"unsupported approval action: {action_value}")
        if action_value == ApprovalAction.EDIT.value:
            if edited_payload is None:
                raise ValueError("edited_plan is required for action='edit'")
            edited = ResearchPlan.model_validate(edited_payload)
            if edited.plan_version != plan.plan_version:
                raise ValueError(
                    "an edited plan must preserve the current plan version"
                )
            if not 2 <= len(edited.tasks) <= self.max_initial_tasks:
                raise ValueError(
                    "an edited initial plan must contain between 2 and "
                    f"{self.max_initial_tasks} tasks"
                )
            policy = state.get("policy", RunPolicy())
            if (
                edited.plan_version == 1
                and policy.initial_task_count is not None
                and len(edited.tasks) != policy.initial_task_count
            ):
                raise ValueError(
                    "the edited initial plan must contain exactly "
                    f"{policy.initial_task_count} tasks"
                )
            plan = edited
        lineage = PlanLineage(
            plan_version=plan.plan_version,
            base_plan=plan,
        )
        target = (
            "finalizer"
            if action_value == ApprovalAction.CANCEL.value
            else "schedule_wave"
        )
        reason = {
            ApprovalAction.APPROVE.value: "计划已获批准",
            ApprovalAction.EDIT.value: "编辑后的计划已通过 DAG 校验",
            ApprovalAction.CANCEL.value: "用户取消运行",
        }[action_value]
        route = await self._record_route(
            {**state, "plan": plan},
            from_node="approval",
            to_node=target,
            reason=reason,
            decision=action_value,
        )
        await self._emit(
            state,
            RunEventType.APPROVAL_RESOLVED,
            reason,
            agent="supervisor",
            node="approval",
            plan_version=plan.plan_version,
            status="resolved",
            decision_reason=reason,
            action=action_value,
        )
        return {
            "plan": plan,
            "plans": self._replace_plan(list(state.get("plans", [])), plan),
            "plan_lineage": lineage,
            "plan_lineages": self._replace_lineage(
                list(state.get("plan_lineages", [])), lineage
            ),
            "ready_tasks": [],
            "final_status": None,
            "requested_final_status": (
                RunStatus.CANCELLED.value
                if action_value == ApprovalAction.CANCEL.value
                else None
            ),
            "route_history": [route],
        }

    @staticmethod
    def _after_approval(state: AgentState) -> str:
        if state.get("requested_final_status") == RunStatus.CANCELLED.value:
            return "finalizer"
        return "schedule_wave"

    async def _schedule_wave(self, state: AgentState) -> dict[str, Any]:
        plan = state["plan"]
        results = self._current_results(state)
        succeeded = {item.task_id for item in results}
        terminal_failures = {
            error.task_id
            for error in state.get("errors", [])
            if error.plan_version == plan.plan_version
            and error.code == "research_failed"
            and error.task_id is not None
            and error.task_id not in succeeded
        }
        completed = succeeded | terminal_failures
        ready = [
            task
            for task in plan.tasks
            if task.task_id not in completed and set(task.dependencies) <= succeeded
        ]
        if ready:
            routes: list[RouteRecord] = []
            for task in ready:
                await self._emit(
                    state,
                    RunEventType.TASK_SCHEDULED,
                    f"已调度研究任务：{task.title}",
                    agent="supervisor",
                    node="schedule_wave",
                    task_id=task.task_id,
                    plan_version=plan.plan_version,
                    status="scheduled",
                    dependencies=task.dependencies,
                )
                routes.append(
                    await self._record_route(
                        state,
                        from_node="schedule_wave",
                        to_node="researcher",
                        reason=f"任务 {task.task_id} 的依赖均已完成",
                        decision="dispatch",
                    )
                )
            return {"ready_tasks": ready, "route_history": routes}
        route = await self._record_route(
            state,
            from_node="schedule_wave",
            to_node="research_gate",
            reason="没有更多可执行的拓扑任务",
            decision="wave_complete",
        )
        return {"ready_tasks": [], "route_history": [route]}

    def _dispatch_wave(self, state: AgentState) -> str | list[Send]:
        ready = state.get("ready_tasks", [])
        if not ready:
            return "research_gate"
        result_map = {item.task_id: item for item in self._current_results(state)}
        return [
            Send(
                "researcher",
                {
                    "run_id": state["run_id"],
                    "query": state["query"],
                    "task": task,
                    "dependency_results": [
                        result_map[dependency]
                        for dependency in task.dependencies
                        if dependency in result_map
                    ],
                },
            )
            for task in ready
        ]

    async def _researcher(self, state: AgentState) -> dict[str, Any]:
        task = state["task"]
        errors: list[AgentError] = []
        calls = 0
        async with self._research_slots:
            async with self._concurrency_lock:
                active = self._active_researchers.get(state["run_id"], 0) + 1
                self._active_researchers[state["run_id"]] = active
                self._peak_researchers[state["run_id"]] = max(
                    active, self._peak_researchers.get(state["run_id"], 0)
                )
            try:
                for attempt in range(1, self.max_research_attempts + 1):
                    started = perf_counter()
                    calls += 1
                    await self._emit(
                        state,
                        RunEventType.NODE_STARTED,
                        f"Researcher 开始任务：{task.title}",
                        agent="researcher",
                        node="researcher",
                        task_id=task.task_id,
                        plan_version=task.plan_version,
                        attempt=attempt,
                        status="running",
                    )
                    try:
                        result = ResearchResult.model_validate(
                            await self.model.analyze_task(
                                state["query"],
                                task,
                                state.get("dependency_results", []),
                            )
                        )
                        if result.task_id != task.task_id:
                            raise ValueError(
                                f"Researcher returned task {result.task_id}; "
                                f"expected {task.task_id}"
                            )
                        if result.plan_version != task.plan_version:
                            raise ValueError(
                                f"Researcher returned plan {result.plan_version}; "
                                f"expected {task.plan_version}"
                            )
                        if result.attempt != attempt:
                            result = result.model_copy(update={"attempt": attempt})
                        duration_ms = int((perf_counter() - started) * 1000)
                        if result.duration_ms == 0:
                            result = result.model_copy(
                                update={"duration_ms": duration_ms}
                            )
                        await self._emit(
                            state,
                            RunEventType.NODE_SUCCEEDED,
                            f"Researcher 完成任务：{task.title}",
                            agent="researcher",
                            node="researcher",
                            task_id=task.task_id,
                            plan_version=task.plan_version,
                            attempt=attempt,
                            status="succeeded",
                            duration_ms=duration_ms,
                        )
                        await self._emit(
                            state,
                            RunEventType.TASK_COMPLETED,
                            f"研究任务完成：{task.title}",
                            agent="researcher",
                            node="researcher",
                            task_id=task.task_id,
                            plan_version=task.plan_version,
                            attempt=attempt,
                            status="succeeded",
                            duration_ms=result.duration_ms,
                            confidence=result.confidence,
                            result=result.model_dump(mode="json"),
                        )
                        route = await self._record_route(
                            state,
                            from_node="researcher",
                            to_node="wave_join",
                            reason=f"任务 {task.task_id} 已产生有效结果",
                            decision="succeeded",
                            plan_version=task.plan_version,
                            attempt=attempt,
                        )
                        return {
                            "research_results": [result],
                            "errors": errors,
                            "route_history": [route],
                            "model_calls": calls,
                        }
                    except Exception as exc:  # noqa: BLE001
                        retryable = attempt < self.max_research_attempts
                        error = self._agent_error(
                            agent="researcher",
                            node="researcher",
                            code="research_retry" if retryable else "research_failed",
                            exc=exc,
                            retryable=retryable,
                            attempt=attempt,
                            task_id=task.task_id,
                            plan_version=task.plan_version,
                        )
                        errors.append(error)
                        await self._emit(
                            state,
                            RunEventType.NODE_FAILED,
                            f"Researcher 任务失败：{task.title}",
                            agent="researcher",
                            node="researcher",
                            task_id=task.task_id,
                            plan_version=task.plan_version,
                            attempt=attempt,
                            status="retrying" if retryable else "failed",
                            duration_ms=int((perf_counter() - started) * 1000),
                            error=error.message,
                        )
                        if retryable:
                            continue
                        await self._emit(
                            state,
                            RunEventType.TASK_COMPLETED,
                            f"研究任务耗尽重试：{task.title}",
                            agent="researcher",
                            node="researcher",
                            task_id=task.task_id,
                            plan_version=task.plan_version,
                            attempt=attempt,
                            status="failed",
                            duration_ms=int((perf_counter() - started) * 1000),
                            error=error.message,
                        )
                        route = await self._record_route(
                            state,
                            from_node="researcher",
                            to_node="wave_join",
                            reason=(
                                f"任务 {task.task_id} 已耗尽 "
                                f"{self.max_research_attempts} 次尝试"
                            ),
                            decision="failed",
                            plan_version=task.plan_version,
                            attempt=attempt,
                        )
                        return {
                            "errors": errors,
                            "route_history": [route],
                            "model_calls": calls,
                        }
            finally:
                async with self._concurrency_lock:
                    self._active_researchers[state["run_id"]] = max(
                        self._active_researchers.get(state["run_id"], 1) - 1, 0
                    )
        raise AssertionError("unreachable Researcher state")

    async def _wave_join(self, state: AgentState) -> dict[str, Any]:
        route = await self._record_route(
            state,
            from_node="wave_join",
            to_node="schedule_wave",
            reason="本轮并行研究结果已汇合",
            decision="next_wave",
        )
        return {"ready_tasks": [], "route_history": [route]}

    async def _research_gate(self, state: AgentState) -> dict[str, Any]:
        plan = state["plan"]
        successful = len({item.task_id for item in self._current_results(state)})
        ratio = successful / len(plan.tasks)
        if ratio >= self.quorum_ratio:
            target = "critic"
            reason = f"研究 Quorum 达标（{successful}/{len(plan.tasks)}）"
            decision = "quorum_met"
            update: dict[str, Any] = {}
            if successful < len(plan.tasks):
                update["warnings"] = [
                    (
                        f"{len(plan.tasks) - successful} 个研究任务未成功，"
                        "已按 Quorum 规则继续"
                    )
                ]
        elif state.get("replan_count", 0) < self.max_replans:
            target = "planner"
            reason = (
                f"研究 Quorum 未达 {self.quorum_ratio:.0%}"
                f"（{successful}/{len(plan.tasks)}），触发重规划"
            )
            decision = "replan"
            update = {
                "replan_count": state.get("replan_count", 0) + 1,
                "next_plan_version": plan.plan_version + 1,
            }
        else:
            target = "finalizer"
            reason = (
                f"重规划后研究 Quorum 仍未达 {self.quorum_ratio:.0%}"
                f"（{successful}/{len(plan.tasks)}）"
            )
            decision = "failed"
            error = AgentError(
                agent="supervisor",
                node="research_gate",
                code="quorum_not_met",
                message=reason,
                retryable=False,
                plan_version=plan.plan_version,
            )
            update = {
                "errors": [error],
                "fatal_error": reason,
                "requested_final_status": RunStatus.FAILED.value,
            }
        route = await self._record_route(
            state,
            from_node="research_gate",
            to_node=target,
            reason=reason,
            decision=decision,
        )
        return {**update, "route_history": [route]}

    @staticmethod
    def _after_research_gate(state: AgentState) -> str:
        return state["route_history"][-1].to_node

    async def _critic(self, state: AgentState) -> dict[str, Any]:
        started = perf_counter()
        plan = state["plan"]
        results = self._current_results(state)
        await self._emit(
            state,
            RunEventType.NODE_STARTED,
            "Critic 正在审查研究覆盖度与冲突",
            agent="critic",
            node="critic",
            plan_version=plan.plan_version,
            status="running",
        )
        try:
            context = self._review_context(state)
            decision = CritiqueDecision.model_validate(
                await self.model.review_research(
                    state["query"],
                    plan,
                    results,
                    context=context,
                )
            )
            decision.validate_for(context)
            if decision.plan_version != plan.plan_version:
                raise ValueError("Critic decision references the wrong plan version")
            update: dict[str, Any] = {
                "critique_history": [decision],
                "review_contexts": [context],
                "model_calls": 1,
            }
            if decision.decision is CritiqueRoute.ACCEPT:
                target, reason = "synthesizer", decision.rationale
                update["synthesis_mode"] = "fresh"
            elif decision.decision is CritiqueRoute.SUPPLEMENT:
                if state.get("supplement_rounds", 0) < self.max_supplement_rounds:
                    if len(decision.supplemental_tasks) > self.max_supplement_tasks:
                        raise ValueError(
                            "Critic supplement exceeds the configured task budget"
                        )
                    lineage = state.get("plan_lineage")
                    if not isinstance(lineage, PlanLineage):
                        lineage = PlanLineage(
                            plan_version=plan.plan_version,
                            base_plan=plan,
                        )
                    expanded_lineage = lineage.with_supplement(
                        decision.supplemental_tasks,
                        rationale=decision.rationale,
                    )
                    expanded = expanded_lineage.active_plan()
                    if len(expanded.tasks) > self.max_tasks_per_plan:
                        raise ValueError(
                            "supplement exceeds the configured per-plan task budget"
                        )
                    target, reason = "schedule_wave", decision.rationale
                    update.update(
                        {
                            "plan": expanded,
                            "plans": self._replace_plan(
                                list(state.get("plans", [])), expanded
                            ),
                            "plan_lineage": expanded_lineage,
                            "plan_lineages": self._replace_lineage(
                                list(state.get("plan_lineages", [])),
                                expanded_lineage,
                            ),
                            "supplement_rounds": state.get("supplement_rounds", 0) + 1,
                            "ready_tasks": [],
                        }
                    )
                else:
                    target = "synthesizer"
                    reason = "Critic 再次请求补充研究，但补充预算已耗尽"
                    update.update({"warnings": [reason], "synthesis_mode": "fresh"})
            elif state.get("replan_count", 0) < self.max_replans:
                target, reason = "planner", decision.rationale
                update.update(
                    {
                        "replan_count": state.get("replan_count", 0) + 1,
                        "next_plan_version": plan.plan_version + 1,
                    }
                )
            else:
                target = "synthesizer"
                reason = "Critic 请求重规划，但全局重规划预算已耗尽"
                update.update({"warnings": [reason], "synthesis_mode": "fresh"})
            route = await self._record_route(
                state,
                from_node="critic",
                to_node=target,
                reason=reason,
                decision=decision.decision.value,
            )
            await self._emit(
                state,
                RunEventType.REVIEW_DECIDED,
                "Critic 已完成研究审查",
                agent="critic",
                node="critic",
                plan_version=plan.plan_version,
                status="succeeded",
                duration_ms=int((perf_counter() - started) * 1000),
                decision_reason=reason,
                decision=decision.decision.value,
                review_context=context.model_dump(mode="json"),
                replan_reason=(
                    decision.replan_reason.value
                    if decision.replan_reason is not None
                    else None
                ),
                issues=[item.model_dump(mode="json") for item in decision.issues],
                supplemental_tasks=[
                    task.model_dump(mode="json")
                    for task in decision.supplemental_tasks
                ],
                active_plan=(
                    update.get("plan", plan).model_dump(mode="json")
                    if isinstance(update.get("plan", plan), ResearchPlan)
                    else plan.model_dump(mode="json")
                ),
                plan_lineage=(
                    update["plan_lineage"].model_dump(mode="json")
                    if isinstance(update.get("plan_lineage"), PlanLineage)
                    else None
                ),
            )
            update["route_history"] = [route]
            return update
        except Exception as exc:  # noqa: BLE001
            update = await self._fatal_update(
                state, agent="critic", node="critic", exc=exc, started=started
            )
            if "context" in locals():
                update["review_contexts"] = [context]
            update["model_calls"] = 1
            return update

    @staticmethod
    def _after_critic(state: AgentState) -> str:
        if state.get("fatal_error"):
            return "finalizer"
        return state["route_history"][-1].to_node

    async def _synthesizer(self, state: AgentState) -> dict[str, Any]:
        started = perf_counter()
        plan = state["plan"]
        results = self._current_results(state)
        drafts = state.get("draft_versions", [])
        version = len(drafts) + 1
        mode = state.get("synthesis_mode", "fresh")
        await self._emit(
            state,
            RunEventType.NODE_STARTED,
            "Synthesizer 正在生成报告草稿",
            agent="synthesizer",
            node="synthesizer",
            plan_version=plan.plan_version,
            attempt=version,
            status="running",
        )
        try:
            if mode == "revise":
                if not drafts or not state.get("quality_history"):
                    raise ValueError("revision requires a draft and a quality decision")
                raw = await self.model.revise_report(
                    state["query"],
                    drafts[-1],
                    state["quality_history"][-1],
                    results,
                )
            else:
                raw = await self.model.synthesize_report(
                    state["query"], plan, results, draft_version=version
                )
            draft = DraftVersion.model_validate(raw)
            if draft.version != version:
                raise ValueError(
                    f"Synthesizer returned draft version {draft.version}; expected {version}"
                )
            if draft.plan_version != plan.plan_version:
                raise ValueError("Synthesizer draft references the wrong plan version")
            unknown_ids = set(draft.based_on_task_ids) - {
                result.task_id for result in results
            }
            if unknown_ids:
                raise ValueError(
                    f"draft references unavailable task results: {sorted(unknown_ids)}"
                )
            route = await self._record_route(
                state,
                from_node="synthesizer",
                to_node="quality_gate",
                reason=f"第 {version} 版草稿已生成",
                decision=mode,
            )
            await self._emit(
                state,
                RunEventType.NODE_SUCCEEDED,
                "Synthesizer 已生成报告草稿",
                agent="synthesizer",
                node="synthesizer",
                plan_version=plan.plan_version,
                attempt=version,
                status="succeeded",
                duration_ms=int((perf_counter() - started) * 1000),
            )
            return {
                "draft_versions": [draft],
                "synthesis_mode": "fresh",
                "route_history": [route],
                "model_calls": 1,
            }
        except Exception as exc:  # noqa: BLE001
            update = await self._fatal_update(
                state,
                agent="synthesizer",
                node="synthesizer",
                exc=exc,
                started=started,
            )
            update["model_calls"] = 1
            return update

    @staticmethod
    def _after_synthesizer(state: AgentState) -> str:
        return "finalizer" if state.get("fatal_error") else "quality_gate"

    async def _quality_gate(self, state: AgentState) -> dict[str, Any]:
        started = perf_counter()
        plan = state["plan"]
        draft = state["draft_versions"][-1]
        results = self._current_results(state)
        await self._emit(
            state,
            RunEventType.NODE_STARTED,
            "QualityGate 正在验收最终报告",
            agent="quality_gate",
            node="quality_gate",
            plan_version=plan.plan_version,
            attempt=draft.version,
            status="running",
        )
        try:
            decision = QualityDecision.model_validate(
                await self.model.evaluate_report(state["query"], draft, results)
            )
            if decision.plan_version != plan.plan_version:
                raise ValueError(
                    "QualityGate decision references the wrong plan version"
                )
            if decision.draft_version != draft.version:
                raise ValueError(
                    "QualityGate decision references the wrong draft version"
                )
            update: dict[str, Any] = {"quality_history": [decision], "model_calls": 1}
            if decision.decision is QualityRoute.ACCEPT:
                if decision.score < self.quality_threshold:
                    raise ValueError(
                        "QualityGate acceptance score is below "
                        f"{self.quality_threshold}"
                    )
                target, reason = "finalizer", decision.rationale
                update["requested_final_status"] = RunStatus.COMPLETED.value
            elif decision.decision is QualityRoute.REVISE:
                if state.get("revision_count", 0) < self.max_revisions:
                    target, reason = "synthesizer", decision.rationale
                    update.update(
                        {
                            "revision_count": state.get("revision_count", 0) + 1,
                            "synthesis_mode": "revise",
                        }
                    )
                else:
                    target = "finalizer"
                    reason = (
                        "报告仍需修订，但 " f"{self.max_revisions} 次修订预算已耗尽"
                    )
                    update.update(
                        {
                            "warnings": [reason],
                            "requested_final_status": RunStatus.COMPLETED_WITH_WARNINGS.value,
                        }
                    )
            elif state.get("replan_count", 0) < self.max_replans:
                target, reason = "planner", decision.rationale
                update.update(
                    {
                        "replan_count": state.get("replan_count", 0) + 1,
                        "next_plan_version": plan.plan_version + 1,
                    }
                )
            else:
                target = "finalizer"
                reason = "QualityGate 请求重规划，但全局重规划预算已耗尽"
                update.update(
                    {
                        "warnings": [reason],
                        "requested_final_status": RunStatus.COMPLETED_WITH_WARNINGS.value,
                    }
                )
            route = await self._record_route(
                state,
                from_node="quality_gate",
                to_node=target,
                reason=reason,
                decision=decision.decision.value,
                attempt=draft.version,
            )
            await self._emit(
                state,
                RunEventType.QUALITY_EVALUATED,
                "QualityGate 已完成验收",
                agent="quality_gate",
                node="quality_gate",
                plan_version=plan.plan_version,
                attempt=draft.version,
                status="succeeded",
                duration_ms=int((perf_counter() - started) * 1000),
                decision_reason=reason,
                decision=decision.decision.value,
                score=decision.score,
                issues=[item.model_dump(mode="json") for item in decision.issues],
                revision_instructions=decision.revision_instructions,
            )
            update["route_history"] = [route]
            return update
        except Exception as exc:  # noqa: BLE001
            update = await self._fatal_update(
                state,
                agent="quality_gate",
                node="quality_gate",
                exc=exc,
                started=started,
            )
            update["model_calls"] = 1
            return update

    @staticmethod
    def _after_quality_gate(state: AgentState) -> str:
        if state.get("fatal_error"):
            return "finalizer"
        return state["route_history"][-1].to_node

    async def _finalizer(self, state: AgentState) -> dict[str, Any]:
        started = perf_counter()
        requested = state.get("requested_final_status")
        warnings = list(state.get("warnings", []))
        if requested == RunStatus.CANCELLED.value:
            status = RunStatus.CANCELLED
        elif requested == RunStatus.FAILED.value or state.get("fatal_error"):
            status = RunStatus.FAILED
        elif warnings or requested == RunStatus.COMPLETED_WITH_WARNINGS.value:
            status = RunStatus.COMPLETED_WITH_WARNINGS
        else:
            status = RunStatus.COMPLETED
        drafts = state.get("draft_versions", [])
        report = drafts[-1].content if drafts else None
        plan_pairs = {
            (plan.plan_version, task.task_id)
            for plan in state.get("plans", [])
            for task in plan.tasks
        }
        success_pairs = {
            (result.plan_version, result.task_id)
            for result in state.get("research_results", [])
        }
        finished_at = datetime.now(UTC)
        started_at = state.get("started_at", finished_at)
        metrics = ExecutionMetrics(
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(int((finished_at - started_at).total_seconds() * 1000), 0),
            total_tasks=len(plan_pairs),
            successful_tasks=len(success_pairs & plan_pairs),
            failed_tasks=len(plan_pairs - success_pairs),
            peak_concurrency=min(
                self._peak_researchers.get(state["run_id"], 0), self.max_concurrency
            ),
            plan_versions=max(len({version for version, _ in plan_pairs}), 1),
            supplement_rounds=state.get("supplement_rounds", 0),
            replan_count=state.get("replan_count", 0),
            revision_count=state.get("revision_count", 0),
            model_calls=state.get("model_calls", 0),
        )
        route = await self._record_route(
            state,
            from_node="finalizer",
            to_node="end",
            reason=f"运行进入终态：{status.value}",
            decision=status.value,
        )
        await self._emit(
            state,
            RunEventType.NODE_SUCCEEDED,
            "Finalizer 已固化运行结果",
            agent="finalizer",
            node="finalizer",
            plan_version=self._plan_version(state),
            status=status.value,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        return {
            "final_status": status.value,
            "report": report,
            "metrics": metrics,
            "route_history": [route],
        }
