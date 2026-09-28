from __future__ import annotations

import pytest
from atlasflow.agents.contracts import (
    DraftVersion,
    QualityDecision,
    QualityRoute,
    ResearchPlan,
    ResearchResult,
    ResearchTask,
    Severity,
)
from atlasflow.agents.report_quality import apply_report_audit, audit_report
from atlasflow.agents.tool_runtime import ToolRuntime
from atlasflow.schemas import Evidence, RunStatus

from backend.tests.fakes import FakeModelGateway, make_test_container


def _research(*, fresh: bool = False) -> tuple[ResearchPlan, list[ResearchResult]]:
    tasks = [
        ResearchTask(
            task_id=f"T{number}",
            title=f"任务 {number}",
            objective=f"研究维度 {number}",
            success_criteria=["给出结果"],
            priority=1,
            dependencies=[],
            plan_version=1,
            requires_fresh_data=fresh and number == 1,
            required_capabilities=["web_search"] if fresh and number == 1 else [],
        )
        for number in (1, 2)
    ]
    plan = ResearchPlan(plan_version=1, rationale="报告验收", tasks=tasks)
    results = [
        ResearchResult(
            task_id=task.task_id,
            plan_version=1,
            summary="完成",
            findings=["发现"],
            confidence=0.8,
        )
        for task in tasks
    ]
    return plan, results


def _accepted() -> QualityDecision:
    return QualityDecision(
        decision=QualityRoute.ACCEPT,
        plan_version=1,
        draft_version=1,
        score=92,
        rationale="模型认为可以验收",
    )


def test_report_audit_keeps_simple_arithmetic_without_external_citations() -> None:
    plan, results = _research()
    report = (
        "## 摘要\n\n1+1=2。 [task:T1]\n\n"
        "## 分析\n\n直接相加。\n\n"
        "## 局限\n\n无外部数据依赖。\n\n"
        "## 来源\n\n由题目与计算直接推导。"
    )
    assert audit_report(report, plan, results, [], []) == []
    assert apply_report_audit(_accepted(), [], threshold=80).decision is QualityRoute.ACCEPT


def test_report_audit_rejects_fenced_fake_headings_and_fabricated_links() -> None:
    plan, results = _research()
    evidence = [
        Evidence(source_id="s1", title="真实来源", content="数据", uri="https://example.test/real")
    ]
    report = (
        "```markdown\n## 摘要\n## 来源\n## 局限\n```\n\n"
        "结论 [task:UNKNOWN] [来源](https://example.test/invented)"
    )
    issues = audit_report(report, plan, results, evidence, [])
    codes = {item.code for item in issues}
    assert {
        "report_missing_sections",
        "report_unknown_task_reference",
        "report_missing_task_reference",
        "report_unknown_source",
        "report_missing_source",
    } <= codes
    decision = apply_report_audit(_accepted(), issues, threshold=80)
    assert decision.decision is QualityRoute.REVISE
    assert decision.score < 80
    assert decision.revision_instructions


def test_fresh_data_without_source_cannot_be_accepted() -> None:
    plan, results = _research(fresh=True)
    report = "## 摘要\n\n当前值未知。[task:T1]\n\n## 局限\n\n未核实。\n\n## 来源\n\n无"
    issues = audit_report(report, plan, results, [], [])
    assert [item.code for item in issues] == ["report_missing_fresh_evidence"]
    assert issues[0].severity is Severity.CRITICAL
    decision = apply_report_audit(_accepted(), issues, threshold=80)
    assert decision.decision is QualityRoute.REPLAN
    assert decision.score < 80


def test_reference_appendix_joins_existing_source_section() -> None:
    report = "## 摘要\n\n结论。\n\n## 来源\n\n原有说明。\n\n## 附录\n\n备注。"
    evidence = [
        Evidence(source_id="s1", title="证据", content="信息", uri="https://example.test/data")
    ]
    updated = ToolRuntime.append_references(report, evidence, [])
    assert updated.count("## 来源") == 1
    assert updated.index("https://example.test/data") < updated.index("## 附录")


@pytest.mark.asyncio
async def test_workflow_routes_accepted_but_invalid_draft_through_revision() -> None:
    class InvalidFirstDraft(FakeModelGateway):
        async def synthesize_report(self, *args, **kwargs) -> DraftVersion:
            draft = await super().synthesize_report(*args, **kwargs)
            return draft.model_copy(
                update={"content": "# 重复的封面标题\n\n含虚构链接 [来源](https://invented.test/) [task:p1-t1]"}
            )

    container = make_test_container(model=InvalidFirstDraft())
    run = await container.run_service.execute_and_wait("检查成稿结构")
    assert run.status is RunStatus.COMPLETED
    assert run.metrics.revision_count == 1
    assert [item.decision for item in run.quality_history] == [
        QualityRoute.REVISE,
        QualityRoute.ACCEPT,
    ]
    assert "report_unknown_source" in {
        issue.code for issue in run.quality_history[0].issues
    }


@pytest.mark.asyncio
async def test_workflow_keeps_unverified_draft_unaccepted_when_revision_exhausted() -> None:
    class InventedSource(FakeModelGateway):
        async def synthesize_report(self, *args, **kwargs) -> DraftVersion:
            draft = await super().synthesize_report(*args, **kwargs)
            return draft.model_copy(
                update={"content": draft.content + "\n\n[虚构来源](https://invented.test/)"}
            )

    container = make_test_container(model=InventedSource())
    container.run_service.workflow.max_revisions = 0
    run = await container.run_service.execute_and_wait("检查未经核实来源")
    assert run.status is RunStatus.COMPLETED_WITH_WARNINGS, run.error
    assert run.quality_history[-1].decision is QualityRoute.REVISE
    assert any(
        issue.code == "report_unknown_source"
        for issue in run.quality_history[-1].issues
    )
