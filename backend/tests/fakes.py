from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import TypeVar

from atlasflow.agents.contracts import (
    CritiqueDecision,
    CritiqueRoute,
    DraftVersion,
    QualityDecision,
    QualityRoute,
    ResearchPlan,
    ResearchResult,
    ResearchTask,
    ReviewIssue,
    Severity,
)
from atlasflow.bootstrap import ProviderBundle, build_container
from atlasflow.config import Settings
from atlasflow.providers import RerankResult
from atlasflow.schemas import Evidence
from atlasflow.tools import BaseTool, RiskLevel, ToolContext, ToolResult
from pydantic import BaseModel

T = TypeVar("T")


class FakeEmbeddingProvider:
    async def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * 16
            for character in text.lower():
                digest = hashlib.sha256(character.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:2], "big") % len(vector)] += 1.0
            vectors.append(vector)
        return vectors


class FakeRerankProvider:
    async def rerank(
        self, query: str, documents: list[str], *, top_n: int
    ) -> list[RerankResult]:
        query_characters = set(query.lower())
        scored = [
            RerankResult(
                index=index,
                score=len(query_characters.intersection(document.lower()))
                / max(len(query_characters), 1),
            )
            for index, document in enumerate(documents)
        ]
        return sorted(scored, key=lambda item: item.score, reverse=True)[:top_n]


@dataclass(slots=True)
class GatewayScenario:
    """Deterministic knobs for exercising every orchestration branch offline."""

    initial_task_count: int = 3
    plan_task_counts: dict[int, int] = field(default_factory=dict)
    linear_dependencies: bool = False
    research_delay_seconds: float = 0.02
    transient_research_failures: dict[str, int] = field(default_factory=dict)
    permanent_research_failures: set[str] = field(default_factory=set)
    critique_routes: list[CritiqueRoute] = field(default_factory=list)
    quality_routes: list[QualityRoute] = field(default_factory=list)
    quality_scores: list[int] = field(default_factory=list)
    supplement_task_count: int = 1
    method_failures: dict[str, int] = field(default_factory=dict)


class FakeModelGateway:
    """Scenario-driven six-method gateway with observable Researcher concurrency."""

    def __init__(self, scenario: GatewayScenario | None = None) -> None:
        self.scenario = scenario or GatewayScenario()
        self.call_counts: dict[str, int] = {}
        self.research_attempts: dict[str, int] = {}
        self.active_researchers = 0
        self.peak_researchers = 0
        self._concurrency_lock = asyncio.Lock()

    async def create_plan(self, query: str, plan_version: int = 1) -> ResearchPlan:
        del query
        self._called("create_plan")
        self._maybe_fail("create_plan")
        count = self.scenario.plan_task_counts.get(
            plan_version, self.scenario.initial_task_count
        )
        tasks: list[ResearchTask] = []
        for index in range(1, count + 1):
            task_id = f"p{plan_version}-t{index}"
            dependencies = (
                [f"p{plan_version}-t{index - 1}"]
                if self.scenario.linear_dependencies and index > 1
                else []
            )
            tasks.append(
                ResearchTask(
                    task_id=task_id,
                    title=f"研究任务 {index}",
                    objective=f"完成问题的第 {index} 个研究维度",
                    success_criteria=[f"产出维度 {index} 的明确结论"],
                    priority=min(index, 5),
                    dependencies=dependencies,
                    plan_version=plan_version,
                )
            )
        return ResearchPlan(
            plan_version=plan_version,
            rationale=f"测试场景计划 v{plan_version}",
            tasks=tasks,
        )

    async def analyze_task(
        self,
        query: str,
        task: ResearchTask,
        dependency_results: Sequence[ResearchResult] = (),
    ) -> ResearchResult:
        del query
        self._called("analyze_task")
        attempt = self.research_attempts.get(task.task_id, 0) + 1
        self.research_attempts[task.task_id] = attempt
        if {result.task_id for result in dependency_results} != set(task.dependencies):
            raise AssertionError(f"dependency results do not match {task.task_id}")

        started = perf_counter()
        async with self._concurrency_lock:
            self.active_researchers += 1
            self.peak_researchers = max(
                self.peak_researchers, self.active_researchers
            )
        try:
            if self.scenario.research_delay_seconds:
                await asyncio.sleep(self.scenario.research_delay_seconds)
            self._maybe_fail("analyze_task")
            transient_failures = self.scenario.transient_research_failures.get(
                task.task_id, 0
            )
            if task.task_id in self.scenario.permanent_research_failures:
                raise RuntimeError(f"permanent research failure: {task.task_id}")
            if attempt <= transient_failures:
                raise RuntimeError(
                    f"transient research failure {attempt}: {task.task_id}"
                )
            return ResearchResult(
                task_id=task.task_id,
                plan_version=task.plan_version,
                summary=f"{task.task_id} 已完成",
                findings=[f"{task.task_id} 的可验证发现"],
                limitations=["离线测试结果，不代表实时资料"],
                confidence=0.9,
                attempt=attempt,
                duration_ms=max(0, round((perf_counter() - started) * 1000)),
            )
        finally:
            async with self._concurrency_lock:
                self.active_researchers -= 1

    async def review_research(
        self,
        query: str,
        plan: ResearchPlan,
        results: Sequence[ResearchResult],
    ) -> CritiqueDecision:
        del query, results
        index = self._called("review_research") - 1
        self._maybe_fail("review_research")
        route = self._sequence_value(
            self.scenario.critique_routes, index, CritiqueRoute.ACCEPT
        )
        supplemental_tasks: list[ResearchTask] = []
        if route is CritiqueRoute.SUPPLEMENT:
            start = len(plan.tasks) + 1
            for offset in range(self.scenario.supplement_task_count):
                number = start + offset
                supplemental_tasks.append(
                    ResearchTask(
                        task_id=f"p{plan.plan_version}-t{number}",
                        title=f"补充任务 {number}",
                        objective="补齐 Critic 指出的研究缺口",
                        success_criteria=["缺口得到明确回答"],
                        priority=1,
                        dependencies=[],
                        plan_version=plan.plan_version,
                    )
                )
        issues = []
        if route is not CritiqueRoute.ACCEPT:
            issues = [
                ReviewIssue(
                    severity=Severity.WARNING,
                    code="research_gap",
                    message="研究覆盖仍有缺口",
                    task_id=None,
                    recommendation="补充研究或重新规划",
                )
            ]
        return CritiqueDecision(
            decision=route,
            plan_version=plan.plan_version,
            rationale=f"Critic 选择 {route.value}",
            issues=issues,
            supplemental_tasks=supplemental_tasks,
        )

    async def synthesize_report(
        self,
        query: str,
        plan: ResearchPlan,
        results: Sequence[ResearchResult],
        draft_version: int = 1,
    ) -> DraftVersion:
        self._called("synthesize_report")
        self._maybe_fail("synthesize_report")
        references = " ".join(f"[task:{result.task_id}]" for result in results)
        return DraftVersion(
            version=draft_version,
            plan_version=plan.plan_version,
            content=f"# 测试报告\n\n{query}\n\n{references}",
            based_on_task_ids=[result.task_id for result in results],
        )

    async def evaluate_report(
        self,
        query: str,
        draft: DraftVersion,
        results: Sequence[ResearchResult],
    ) -> QualityDecision:
        del query, results
        index = self._called("evaluate_report") - 1
        self._maybe_fail("evaluate_report")
        route = self._sequence_value(
            self.scenario.quality_routes, index, QualityRoute.ACCEPT
        )
        default_score = {
            QualityRoute.ACCEPT: 90,
            QualityRoute.REVISE: 70,
            QualityRoute.REPLAN: 40,
        }[route]
        score = self._sequence_value(
            self.scenario.quality_scores, index, default_score
        )
        issues = []
        if route is not QualityRoute.ACCEPT:
            issues = [
                ReviewIssue(
                    severity=(
                        Severity.CRITICAL
                        if route is QualityRoute.REPLAN
                        else Severity.WARNING
                    ),
                    code="quality_gap",
                    message="报告未达到验收标准",
                    task_id=None,
                    recommendation="按质量意见修订",
                )
            ]
        return QualityDecision(
            decision=route,
            plan_version=draft.plan_version,
            draft_version=draft.version,
            score=score,
            rationale=f"QualityGate 选择 {route.value}",
            issues=issues,
            revision_instructions=(
                ["补充缺失分析并改善结构"]
                if route is QualityRoute.REVISE
                else []
            ),
        )

    async def revise_report(
        self,
        query: str,
        draft: DraftVersion,
        decision: QualityDecision,
        results: Sequence[ResearchResult],
    ) -> DraftVersion:
        del decision
        self._called("revise_report")
        self._maybe_fail("revise_report")
        references = " ".join(f"[task:{result.task_id}]" for result in results)
        return DraftVersion(
            version=draft.version + 1,
            plan_version=draft.plan_version,
            content=f"# 修订测试报告\n\n{query}\n\n{references}",
            based_on_task_ids=[result.task_id for result in results],
        )

    def _called(self, method: str) -> int:
        count = self.call_counts.get(method, 0) + 1
        self.call_counts[method] = count
        return count

    def _maybe_fail(self, method: str) -> None:
        remaining = self.scenario.method_failures.get(method, 0)
        if remaining <= 0:
            return
        self.scenario.method_failures[method] = remaining - 1
        raise RuntimeError(f"configured {method} failure")

    @staticmethod
    def _sequence_value(values: Sequence[T], index: int, default: T) -> T:
        return values[index] if index < len(values) else default


class EmptyArguments(BaseModel):
    pass


class FakeWebSearchTool(BaseTool):
    name = "web_search"
    description = "Test-only web search double."
    risk_level = RiskLevel.LOW
    arguments_model = EmptyArguments

    async def run(self, arguments: EmptyArguments, context: ToolContext) -> ToolResult:
        return ToolResult(
            success=True,
            evidence=[
                Evidence(
                    source_id="test-web:1",
                    title="Test web source",
                    content="Test-only external evidence.",
                    uri="https://example.test/source",
                    score=1.0,
                    metadata={"provider": "test-double"},
                )
            ],
        )


def make_test_settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_provider="openrouter",
        embedding_provider="openrouter",
        rerank_provider="openrouter",
        search_provider="openrouter",
        llm_model="test/model",
        embedding_model="test/embedding",
        rerank_model="test/rerank",
        llm_api_key="test-key",
        embedding_api_key="test-key",
        rerank_api_key="test-key",
        tool_timeout_seconds=2,
    )


def make_test_container(
    *,
    include_web: bool = True,
    model: FakeModelGateway | None = None,
    scenario: GatewayScenario | None = None,
):
    if model is not None and scenario is not None:
        raise ValueError("pass model or scenario, not both")
    gateway = model or FakeModelGateway(scenario)
    providers = ProviderBundle(
        model=gateway,
        embedding=FakeEmbeddingProvider(),
        reranker=FakeRerankProvider(),
        web_search_tool=FakeWebSearchTool() if include_web else None,
    )
    return build_container(make_test_settings(), providers=providers)
