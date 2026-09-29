from __future__ import annotations

import pytest
from atlasflow.agents.contracts import (
    CritiqueDecision,
    CritiqueRoute,
    PlanLineage,
    QualityDecision,
    QualityRoute,
    ReplanReason,
    ResearchPlan,
    ResearchTask,
    ReviewContext,
    ReviewIssue,
    RunPolicy,
    Severity,
)
from atlasflow.schemas import ApprovalAction, ApprovalRequest, RunRecord, RunStatus
from pydantic import ValidationError


def _task(task_id: str, dependencies: list[str]) -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        title=f"任务 {task_id}",
        objective="验证结构化契约",
        success_criteria=["契约校验通过"],
        priority=1,
        dependencies=dependencies,
        plan_version=1,
    )


def test_research_plan_rejects_cycles() -> None:
    with pytest.raises(ValidationError, match="acyclic"):
        ResearchPlan(
            plan_version=1,
            rationale="循环依赖应被拒绝",
            tasks=[_task("T1", ["T2"]), _task("T2", ["T1"])],
        )


def test_default_tool_budgets_allow_five_per_decision_and_twenty_per_run() -> None:
    policy = RunPolicy()
    assert policy.max_tool_calls_per_turn == 5
    assert policy.max_tool_calls_per_run == 20


def test_quality_accept_requires_score_at_least_eighty() -> None:
    with pytest.raises(ValidationError, match="at least 80"):
        QualityDecision(
            decision=QualityRoute.ACCEPT,
            plan_version=1,
            draft_version=1,
            score=79,
            rationale="分数不足",
            issues=[],
            revision_instructions=[],
        )


def test_plan_lineage_keeps_the_base_plan_separate_from_supplements() -> None:
    base_plan = ResearchPlan(
        plan_version=1,
        rationale="两个初始研究维度",
        tasks=[_task("T1", []), _task("T2", [])],
    )

    lineage = PlanLineage(plan_version=1, base_plan=base_plan).with_supplement(
        [_task("T3", [])],
        rationale="补充游览建议",
    )

    assert lineage.base_plan.task_ids == ("T1", "T2")
    assert lineage.supplements[0].tasks[0].task_id == "T3"
    assert lineage.active_plan().task_ids == ("T1", "T2", "T3")
    assert lineage.plan_version == 1
    assert lineage.revision == 1


def test_replan_without_a_critical_issue_is_semantically_rejected() -> None:
    policy = RunPolicy(initial_task_count=2)
    context = ReviewContext(
        review_round=1,
        plan_version=1,
        plan_revision=0,
        initial_task_ids=["T1", "T2"],
        supplemental_task_ids=[],
        completed_task_ids=["T1", "T2"],
        failed_task_ids=[],
        supplement_rounds_used=0,
        supplement_rounds_remaining=1,
        current_task_count=2,
        expected_task_count=2,
        policy=policy,
    )
    decision = CritiqueDecision(
        decision=CritiqueRoute.REPLAN,
        plan_version=1,
        rationale="错误地把任务数量当作全局规划问题",
        issues=[
            ReviewIssue(
                severity=Severity.WARNING,
                code="task_count_mismatch",
                message="当前计划包含两个任务",
                recommendation="重新规划",
            )
        ],
        replan_reason=ReplanReason.INVALID_DECOMPOSITION,
    )

    with pytest.raises(ValueError, match="require a critical issue"):
        decision.validate_for(context)

    mismatched = decision.model_copy(
        update={
            "issues": [
                ReviewIssue(
                    severity=Severity.CRITICAL,
                    code="task_count_mismatch",
                    message="当前三个任务被误认为超量",
                    recommendation="忽略合法补充任务的数量变化",
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="matching critical issue code"):
        mismatched.validate_for(context)


def test_edit_approval_requires_a_plan() -> None:
    with pytest.raises(ValidationError, match="edited_plan is required"):
        ApprovalRequest(action=ApprovalAction.EDIT)


def test_terminal_run_invariants_are_enforced() -> None:
    with pytest.raises(ValidationError, match="non-empty report"):
        RunRecord(query="验证终态", status=RunStatus.COMPLETED)

    with pytest.raises(ValidationError, match="at least one warning"):
        RunRecord(
            query="验证降级终态",
            status=RunStatus.COMPLETED_WITH_WARNINGS,
            report="# 报告",
        )
