from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class ContractModel(BaseModel):
    """Base settings shared by every value crossing an Agent boundary."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class CritiqueRoute(StrEnum):
    ACCEPT = "accept"
    SUPPLEMENT = "supplement"
    REPLAN = "replan"


class QualityRoute(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"
    REPLAN = "replan"


class ReplanReason(StrEnum):
    """Global plan failures that can justify discarding the current plan."""

    INVALID_DECOMPOSITION = "invalid_decomposition"
    UNRESOLVED_CRITICAL_GAP = "unresolved_critical_gap"
    IRRECONCILABLE_CONFLICT = "irreconcilable_conflict"
    DEPENDENCY_DEAD_END = "dependency_dead_end"


class RunPolicy(ContractModel):
    """Deterministic orchestration requirements kept separate from the query."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    initial_task_count: int | None = Field(default=None, ge=2, le=5)
    required_supplement_rounds: int = Field(default=0, ge=0, le=1)
    supplement_task_count: int | None = Field(default=None, ge=1, le=2)
    replan_requires_critical_issue: bool = True


class ResearchTask(ContractModel):
    task_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=2_000)
    success_criteria: list[str] = Field(min_length=1, max_length=10)
    priority: int = Field(ge=1, le=5)
    dependencies: list[str] = Field(default_factory=list, max_length=6)
    plan_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        if any(not criterion for criterion in self.success_criteria):
            raise ValueError("task success criteria must not be blank")
        if len(self.success_criteria) != len(set(self.success_criteria)):
            raise ValueError("task success criteria must be unique")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("task dependencies must be unique")
        if self.task_id in self.dependencies:
            raise ValueError("a task cannot depend on itself")
        return self


class ResearchPlan(ContractModel):
    """A validated task DAG.

    Seven tasks deliberately accommodates the largest allowed initial plan (five)
    plus one Critic supplement round (two).
    """

    plan_version: int = Field(ge=1)
    rationale: str = Field(min_length=1, max_length=2_000)
    tasks: list[ResearchTask] = Field(min_length=2, max_length=7)

    @model_validator(mode="after")
    def validate_task_dag(self) -> Self:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("research task ids must be unique")

        known_ids = set(task_ids)
        for task in self.tasks:
            if task.plan_version != self.plan_version:
                raise ValueError(
                    f"task {task.task_id!r} belongs to plan version "
                    f"{task.plan_version}, expected {self.plan_version}"
                )
            missing = set(task.dependencies) - known_ids
            if missing:
                raise ValueError(
                    f"task {task.task_id!r} has unknown dependencies: {sorted(missing)}"
                )

        dependencies = {task.task_id: set(task.dependencies) for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("research task dependencies must be acyclic")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in dependencies[task_id]:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.tasks)

    @property
    def task_map(self) -> dict[str, ResearchTask]:
        return {task.task_id: task for task in self.tasks}

    def with_supplemental_tasks(self, tasks: Sequence[ResearchTask]) -> ResearchPlan:
        supplemental = list(tasks)
        if not 1 <= len(supplemental) <= 2:
            raise ValueError("a supplement round must add one or two tasks")
        return ResearchPlan(
            plan_version=self.plan_version,
            rationale=self.rationale,
            tasks=[*self.tasks, *supplemental],
        )


class SupplementBatch(ContractModel):
    """The immutable delta introduced by one Critic supplement decision."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    round: int = Field(ge=1)
    plan_version: int = Field(ge=1)
    rationale: str = Field(min_length=1, max_length=4_000)
    tasks: list[ResearchTask] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_tasks(self) -> Self:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("supplemental task ids must be unique")
        if any(task.plan_version != self.plan_version for task in self.tasks):
            raise ValueError(
                "supplemental tasks must use the supplemented plan version"
            )
        return self


class PlanLineage(ContractModel):
    """A base plan and its append-only supplement deltas."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    plan_version: int = Field(ge=1)
    base_plan: ResearchPlan
    supplements: list[SupplementBatch] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        if self.base_plan.plan_version != self.plan_version:
            raise ValueError("base plan must use the lineage plan version")

        expected_rounds = list(range(1, len(self.supplements) + 1))
        actual_rounds = [batch.round for batch in self.supplements]
        if actual_rounds != expected_rounds:
            raise ValueError("supplement rounds must be contiguous and start at one")
        if any(batch.plan_version != self.plan_version for batch in self.supplements):
            raise ValueError("supplement batches must use the lineage plan version")

        all_ids = [*self.initial_task_ids, *self.supplemental_task_ids]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError(
                "supplemental task ids must not duplicate existing task ids"
            )

        # Rebuilding the active plan also validates dependencies and cycles across
        # the base plan and every supplement delta.
        self.active_plan()
        return self

    @property
    def revision(self) -> int:
        return len(self.supplements)

    @property
    def initial_task_ids(self) -> tuple[str, ...]:
        return self.base_plan.task_ids

    @property
    def supplemental_task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for batch in self.supplements for task in batch.tasks)

    def active_plan(self) -> ResearchPlan:
        supplemental_tasks = [
            task for batch in self.supplements for task in batch.tasks
        ]
        return ResearchPlan(
            plan_version=self.plan_version,
            rationale=self.base_plan.rationale,
            tasks=[*self.base_plan.tasks, *supplemental_tasks],
        )

    def with_supplement(
        self,
        tasks: Sequence[ResearchTask],
        *,
        rationale: str,
    ) -> PlanLineage:
        batch = SupplementBatch(
            round=self.revision + 1,
            plan_version=self.plan_version,
            rationale=rationale,
            tasks=list(tasks),
        )
        return PlanLineage(
            plan_version=self.plan_version,
            base_plan=self.base_plan,
            supplements=[*self.supplements, batch],
        )


class ResearchResult(ContractModel):
    result_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str = Field(min_length=1, max_length=64)
    plan_version: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=8_000)
    findings: list[str] = Field(min_length=1, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0.0, le=1.0)
    attempt: int = Field(default=1, ge=1)
    duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_content_lists(self) -> Self:
        if any(not finding for finding in self.findings):
            raise ValueError("research findings must not be blank")
        if any(not limitation for limitation in self.limitations):
            raise ValueError("research limitations must not be blank")
        return self


class ReviewIssue(ContractModel):
    severity: Severity
    code: str = Field(
        min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    message: str = Field(min_length=1, max_length=2_000)
    task_id: str | None = Field(default=None, max_length=64)
    recommendation: str = Field(min_length=1, max_length=2_000)


class ReviewContext(ContractModel):
    """Workflow-owned facts supplied to a Critic for one review round."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    review_round: int = Field(ge=1)
    plan_version: int = Field(ge=1)
    plan_revision: int = Field(ge=0, le=1)
    initial_task_ids: list[str] = Field(min_length=2, max_length=5)
    supplemental_task_ids: list[str] = Field(default_factory=list, max_length=2)
    completed_task_ids: list[str] = Field(default_factory=list, max_length=7)
    failed_task_ids: list[str] = Field(default_factory=list, max_length=7)
    supplement_rounds_used: int = Field(ge=0, le=1)
    supplement_rounds_remaining: int = Field(ge=0, le=1)
    current_task_count: int = Field(ge=2, le=7)
    expected_task_count: int = Field(ge=2, le=7)
    policy: RunPolicy = Field(default_factory=RunPolicy)

    @model_validator(mode="after")
    def validate_workflow_facts(self) -> Self:
        id_groups = {
            "initial": self.initial_task_ids,
            "supplemental": self.supplemental_task_ids,
            "completed": self.completed_task_ids,
            "failed": self.failed_task_ids,
        }
        for label, ids in id_groups.items():
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} task ids must be unique")

        initial_ids = set(self.initial_task_ids)
        supplemental_ids = set(self.supplemental_task_ids)
        if initial_ids & supplemental_ids:
            raise ValueError("initial and supplemental task ids must be disjoint")

        completed_ids = set(self.completed_task_ids)
        failed_ids = set(self.failed_task_ids)
        if completed_ids & failed_ids:
            raise ValueError("completed and failed task ids must be disjoint")

        active_ids = initial_ids | supplemental_ids
        unknown_result_ids = (completed_ids | failed_ids) - active_ids
        if unknown_result_ids:
            raise ValueError(
                "review result ids must belong to the active plan: "
                f"{sorted(unknown_result_ids)}"
            )

        if self.plan_revision > self.supplement_rounds_used:
            raise ValueError("plan revision cannot exceed used supplement rounds")
        if self.supplement_rounds_used + self.supplement_rounds_remaining > 1:
            raise ValueError("supplement round budget cannot exceed one")
        if self.plan_revision == 0 and self.supplemental_task_ids:
            raise ValueError("supplemental task ids require a plan revision")
        if self.plan_revision == 1 and not self.supplemental_task_ids:
            raise ValueError("a plan revision requires supplemental task ids")

        if self.current_task_count != len(active_ids):
            raise ValueError("current task count must match the active task ids")

        if (
            self.plan_version == 1
            and self.policy.initial_task_count is not None
            and len(self.initial_task_ids) != self.policy.initial_task_count
        ):
            raise ValueError("initial task ids must satisfy policy.initial_task_count")

        if (
            self.policy.supplement_task_count is not None
            and self.plan_revision
            and len(self.supplemental_task_ids)
            != self.policy.supplement_task_count * self.plan_revision
        ):
            raise ValueError(
                "supplemental task ids must satisfy policy.supplement_task_count"
            )

        expected_initial = (
            self.policy.initial_task_count
            if self.plan_version == 1 and self.policy.initial_task_count is not None
            else len(self.initial_task_ids)
        )
        expected_supplemental = (
            self.policy.supplement_task_count
            if self.policy.supplement_task_count is not None
            else len(self.supplemental_task_ids)
        ) * self.plan_revision
        if self.expected_task_count != expected_initial + expected_supplemental:
            raise ValueError("expected task count is inconsistent with the run policy")
        return self


class CritiqueDecision(ContractModel):
    decision: CritiqueRoute
    plan_version: int = Field(ge=1)
    rationale: str = Field(min_length=1, max_length=4_000)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=20)
    supplemental_tasks: list[ResearchTask] = Field(default_factory=list, max_length=2)
    replan_reason: ReplanReason | None = None

    @model_validator(mode="after")
    def validate_route_payload(self) -> Self:
        if self.decision is CritiqueRoute.SUPPLEMENT:
            if not self.supplemental_tasks:
                raise ValueError("supplement decisions must include one or two tasks")
        elif self.supplemental_tasks:
            raise ValueError(
                "only a supplement decision may include supplemental tasks"
            )

        task_ids = [task.task_id for task in self.supplemental_tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("supplemental task ids must be unique")
        if any(
            task.plan_version != self.plan_version for task in self.supplemental_tasks
        ):
            raise ValueError("supplemental tasks must use the reviewed plan version")

        if self.decision is CritiqueRoute.REPLAN:
            if self.replan_reason is None:
                raise ValueError("replan decisions must include a replan reason")
        elif self.replan_reason is not None:
            raise ValueError("only a replan decision may include a replan reason")
        return self

    def validate_for(self, context: ReviewContext) -> Self:
        """Validate a syntactically valid decision against workflow-owned facts."""

        if self.plan_version != context.plan_version:
            raise ValueError("Critic decision references the wrong plan version")

        required_round_pending = (
            context.supplement_rounds_used < context.policy.required_supplement_rounds
        )
        if required_round_pending and self.decision is not CritiqueRoute.SUPPLEMENT:
            raise ValueError(
                "the run policy requires a supplement round before accept or replan"
            )

        if self.decision is CritiqueRoute.SUPPLEMENT:
            if context.supplement_rounds_remaining < 1:
                raise ValueError(
                    "supplement decision exceeds the remaining round budget"
                )

            expected_count = context.policy.supplement_task_count
            if (
                expected_count is not None
                and len(self.supplemental_tasks) != expected_count
            ):
                raise ValueError(
                    "supplement decision must satisfy policy.supplement_task_count"
                )

            existing_ids = {
                *context.initial_task_ids,
                *context.supplemental_task_ids,
            }
            added_ids = [task.task_id for task in self.supplemental_tasks]
            duplicate_ids = existing_ids.intersection(added_ids)
            if duplicate_ids:
                raise ValueError(
                    "supplemental task ids must be new: " f"{sorted(duplicate_ids)}"
                )

            if context.current_task_count + len(added_ids) > 7:
                raise ValueError("supplement decision exceeds the plan task limit")

        if (
            self.decision is CritiqueRoute.REPLAN
            and context.policy.replan_requires_critical_issue
        ):
            critical_issues = [
                issue for issue in self.issues if issue.severity is Severity.CRITICAL
            ]
            if not critical_issues:
                raise ValueError("replan decisions require a critical issue")
            expected_code = self.replan_reason.value if self.replan_reason else None
            if not any(issue.code == expected_code for issue in critical_issues):
                raise ValueError(
                    "replan reason must be backed by a matching critical issue code"
                )
        return self


class DraftVersion(ContractModel):
    version: int = Field(ge=1)
    plan_version: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=200_000)
    based_on_task_ids: list[str] = Field(default_factory=list, max_length=7)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_task_ids(self) -> Self:
        if len(self.based_on_task_ids) != len(set(self.based_on_task_ids)):
            raise ValueError("draft task ids must be unique")
        return self


class QualityDecision(ContractModel):
    decision: QualityRoute
    plan_version: int = Field(ge=1)
    draft_version: int = Field(ge=1)
    score: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=4_000)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=20)
    revision_instructions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_acceptance_threshold(self) -> Self:
        if self.decision is QualityRoute.ACCEPT:
            if self.score < 80:
                raise ValueError("quality acceptance requires a score of at least 80")
            if any(issue.severity is Severity.CRITICAL for issue in self.issues):
                raise ValueError("quality acceptance cannot contain a critical issue")
        return self


class AgentError(ContractModel):
    agent: str = Field(min_length=1, max_length=80)
    node: str = Field(min_length=1, max_length=80)
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4_000)
    retryable: bool = False
    attempt: int = Field(default=1, ge=1)
    task_id: str | None = Field(default=None, max_length=64)
    plan_version: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utc_now)


class RouteRecord(ContractModel):
    from_node: str = Field(min_length=1, max_length=80)
    to_node: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=2_000)
    decision: str | None = Field(default=None, max_length=80)
    plan_version: int = Field(ge=1)
    attempt: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)


class ExecutionMetrics(ContractModel):
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    total_tasks: int = Field(default=0, ge=0, le=12)
    successful_tasks: int = Field(default=0, ge=0, le=12)
    failed_tasks: int = Field(default=0, ge=0, le=12)
    peak_concurrency: int = Field(default=0, ge=0, le=3)
    plan_versions: int = Field(default=1, ge=1, le=2)
    supplement_rounds: int = Field(default=0, ge=0, le=1)
    replan_count: int = Field(default=0, ge=0, le=1)
    revision_count: int = Field(default=0, ge=0, le=2)
    model_calls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_task_counts(self) -> Self:
        if self.successful_tasks + self.failed_tasks > self.total_tasks:
            raise ValueError("successful and failed tasks cannot exceed total tasks")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        return self


def task_ids(items: Iterable[ResearchResult]) -> list[str]:
    """Return stable, de-duplicated task ids in result order."""

    return list(dict.fromkeys(item.task_id for item in items))
