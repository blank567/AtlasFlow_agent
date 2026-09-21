from __future__ import annotations

import pytest
from atlasflow.agents.contracts import (
    QualityDecision,
    QualityRoute,
    ResearchPlan,
    ResearchTask,
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
