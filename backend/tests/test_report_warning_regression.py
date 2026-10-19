"""Citation loss and non-actionable reviews must not cause endless revisions."""

from __future__ import annotations

import json

import pytest
from atlasflow.agents.contracts import (
    DraftVersion,
    QualityDecision,
    QualityRoute,
    ResearchPlan,
    ResearchResult,
    ResearchTask,
)
from atlasflow.agents.gateway import OpenRouterModelGateway
from atlasflow.agents.report_quality import audit_report, report_audit_context
from atlasflow.agents.report_skill import cited_numbers, render_academic_references, report_prose
from atlasflow.agents.tool_runtime import (
    RequiredToolError,
    ToolBudgetExhausted,
    ToolRuntime,
    ToolStageResult,
)
from atlasflow.agents.workflow import ResearchWorkflow
from atlasflow.providers.openrouter import ProviderRequestError
from atlasflow.schemas import Evidence, RunEventType, RunStatus
from atlasflow.service import RunService

from backend.tests.fakes import FakeModelGateway, GatewayScenario, make_test_container


def research():
    tasks = [
        ResearchTask(
            task_id=f"T{i}",
            title="维度",
            objective="核验结论",
            success_criteria=["结论有证据"],
            priority=1,
            dependencies=[],
            plan_version=1,
        )
        for i in (1, 2)
    ]
    plan = ResearchPlan(plan_version=1, rationale="引用回归", tasks=tasks)
    results = [
        ResearchResult(
            task_id=t.task_id,
            plan_version=1,
            summary="完成",
            findings=["有依据的结论"],
            confidence=0.9,
        )
        for t in tasks
    ]
    evidence = [
        Evidence(
            source_id=f"S{i}",
            title=f"来源{i}",
            content="有依据的结论",
            uri=f"https://example.test/{i}",
        )
        for i in (1, 2)
    ]
    return plan, results, evidence


def report(body="中文结论[1][2]。English claim[1]。"):
    return f"## 摘要\n\n{body}\n\n## 结论\n\n结论成立。\n\n## 局限\n\n仅根据已有证据。\n"


def decision(route="accept", *, issues=None, instructions=None):
    return QualityDecision(
        decision=route,
        plan_version=1,
        draft_version=1,
        score=90 if route == "accept" else 70,
        rationale="报告验收",
        issues=issues or [],
        revision_instructions=instructions or [],
    )


ISSUE = {
    "severity": "warning",
    "code": "claim_support",
    "message": "结论[1]缺少支持",
    "task_id": None,
    "recommendation": "删除无支持结论或补充来源",
}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("经典模型[1]。", {1}),
        ("经典模型 [1]。", {1}),
        ("模型[1][2]。", {1, 2}),
        ("Transformer[2]", {2}),
        ("结论。[1]", {1}),
        (r"示例\[9]与真正引用[1]", {1}),
        ("[9](https://example.test)与真实结论[1]", {1}),
        ("![9](https://example.test/image) 与 `arr[8]` 和结论[1]", {1}),
        ("``inline `arr[8]` code`` 与结论[1]", {1}),
        ("[9]: https://example.test\n结论[1]", {1}),
    ],
)
def test_citations_accept_adjacent_cjk_english_but_ignore_code_links_and_escapes(text, expected):
    assert cited_numbers(text) == expected


def test_fence_mask_preserves_offsets_and_ignores_fake_bibliography_heading():
    text = "````markdown\n## 参考文献\n[9]\n```\n[8]\n````\n结论[1][2]。\n"
    assert len(report_prose(text)) == len(text)
    assert cited_numbers(text) == {1, 2}
    _, _, evidence = research()
    rendered = render_academic_references(text, evidence, [])
    assert "[9]\n```\n[8]" in rendered  # Code is not rewritten as a bibliography.
    assert "[1] 来源1[EB/OL]" in rendered and "[2] 来源2[EB/OL]" in rendered


def test_render_and_audit_agree_and_keep_valid_bibliography_links():
    plan, results, evidence = research()
    rendered = render_academic_references(report(), evidence, [])
    assert cited_numbers(rendered) == {1, 2}
    assert "[1] 来源1[EB/OL]" in rendered and "[2] 来源2[EB/OL]" in rendered
    assert audit_report(rendered, plan, results, evidence, []) == []
    assert render_academic_references(rendered, evidence, []) == rendered
    missing = rendered.replace("[1] 来源1[EB/OL]. [访问链接](https://example.test/1)", "")
    issues = audit_report(missing, plan, results, evidence, [])
    assert "report_citation_missing_reference" in {issue.code for issue in issues}
    metadata = json.loads(report_audit_context(missing, issues).split("：\n", 1)[1])
    assert metadata["body_citations"] == [1, 2] and metadata["missing_reference_entries"] == [1]


def test_unknown_citation_and_real_missing_evidence_are_not_suppressed():
    plan, results, evidence = research()
    rendered = render_academic_references(report("结论[1][99]。"), evidence, [])
    assert "[99]" in rendered.split("## 参考文献")[0]
    assert "report_unknown_citation" in {
        issue.code for issue in audit_report(rendered, plan, results, evidence, [])
    }
    plan.tasks[0].requires_fresh_data = True
    assert "report_missing_fresh_evidence" in {
        issue.code for issue in audit_report(report(), plan, results, [], [])
    }


@pytest.mark.parametrize(
    "route, issues, instructions, error",
    [
        ("revise", [], [], "concrete issues"),
        ("revise", [ISSUE], [], "actionable revision_instructions"),
        ("replan", [ISSUE], [], "critical issue"),
    ],
)
def test_strict_execution_validation_does_not_break_historical_reads(
    route, issues, instructions, error
):
    historical = decision(route, issues=issues, instructions=instructions)
    assert QualityDecision.model_validate(historical.model_dump()).decision.value == route
    with pytest.raises(ValueError, match=error):
        historical.validate_for_execution()


@pytest.mark.parametrize("repair", [True, False])
async def test_gateway_repairs_empty_review_once_without_rewriting_report(repair):
    class Client:
        def __init__(self):
            self.requests = []

        async def chat(self, **kwargs):
            self.requests.append(kwargs)
            payload = decision("revise").model_dump(
                mode="json", exclude={"plan_version", "draft_version"}
            )
            if repair and len(self.requests) > 1:
                payload.update(issues=[ISSUE], revision_instructions=["删除无支持结论或补充来源"])
            return {"content": json.dumps({"verdict": payload}), "_atlasflow_finish_reason": "stop"}

    client = Client()
    gateway = OpenRouterModelGateway(client, "test/model")
    draft = DraftVersion(version=1, plan_version=1, content=report())
    _, results, _ = research()
    if repair:
        reviewed = await gateway.evaluate_report("核验报告", draft, results)
        assert reviewed.decision is QualityRoute.REVISE and reviewed.revision_instructions
    else:
        with pytest.raises(ProviderRequestError, match="concrete issues"):
            await gateway.evaluate_report("核验报告", draft, results)
    assert len(client.requests) == 2
    assert "校验错误" in client.requests[-1]["messages"][-1]["content"]
    assert "文末参考文献允许" in client.requests[0]["messages"][0]["content"]


class OptionalFailureRuntime(ToolRuntime):
    budget_failure = False

    async def gather(self, *, agent, **kwargs):
        if agent == "critic":
            error = ToolBudgetExhausted if self.budget_failure else RequiredToolError
            raise error("test-only optional review tool failure", ToolStageResult(model_calls=1))
        return ToolStageResult()


@pytest.mark.parametrize(
    "accepted, budget_failure, expected",
    [
        (True, False, RunStatus.COMPLETED),
        (False, False, RunStatus.COMPLETED_WITH_WARNINGS),
        (True, True, RunStatus.COMPLETED_WITH_WARNINGS),
    ],
)
async def test_optional_degradation_becomes_history_only_after_acceptance(
    accepted, budget_failure, expected
):
    class CaptureModel(FakeModelGateway):
        def __init__(self):
            super().__init__(
                GatewayScenario(
                    quality_routes=[QualityRoute.ACCEPT if accepted else QualityRoute.REVISE]
                )
            )
            self.quality_context = ""
            self.writing_context = ""

        async def synthesize_report(self, *args, **kwargs):
            self.writing_context = kwargs.get("tool_context", "")
            return await super().synthesize_report(*args, **kwargs)

        async def evaluate_report(self, *args, **kwargs):
            self.quality_context = kwargs.get("tool_context", "")
            return await super().evaluate_report(*args, **kwargs)

    model = CaptureModel()
    container = make_test_container(model=model)
    runtime = OptionalFailureRuntime(
        registry=container.registry,
        client=object(),
        model="no-real-model",
        event_sink=container.store.append_event,
    )
    runtime.budget_failure = budget_failure
    container.run_service.workflow.tool_runtime = runtime
    container.run_service.workflow.max_revisions = 0
    run = await container.run_service.execute_and_wait("测试引用与降级警告")
    assert run.status is expected, run.error
    assert "系统生成的报告格式核验" in model.quality_context
    assert "missing_reference_entries" in model.quality_context
    assert any(
        event.event_type is RunEventType.TOOL_FAILED and event.data.get("optional")
        for event in run.events
    )
    final = next(
        event
        for event in run.events
        if event.agent == "finalizer" and "historical_operational_warnings" in event.data
    )
    if accepted and not budget_failure:
        assert run.warnings == [] and final.data["historical_operational_warnings"]
        assert "可选工具阶段失败" not in model.writing_context
    else:
        assert run.warnings and not final.data["historical_operational_warnings"]
        if budget_failure:
            assert "可选工具阶段失败" in model.writing_context


async def test_empty_custom_quality_decision_does_not_consume_revisions_or_pass():
    class EmptyReview(FakeModelGateway):
        async def evaluate_report(self, query, draft, results, **kwargs):
            return decision("revise").model_copy(update={"draft_version": draft.version})

    container = make_test_container(model=EmptyReview())
    run = await container.run_service.execute_and_wait("测试无有效修订意见")
    assert run.status is RunStatus.FAILED
    assert run.draft_versions and run.report
    assert run.metrics.revision_count == 0
    assert "concrete issues" in run.error


@pytest.mark.parametrize("approval", ["current", "wrong_draft", "wrong_plan", "missing"])
async def test_finalizer_keeps_unclassified_warnings_and_requires_matching_acceptance(approval):
    events = []

    async def collect(run_id, event):
        events.append(event)

    workflow = ResearchWorkflow(model=FakeModelGateway(), event_sink=collect)
    plan, results, _ = research()
    quality = decision()
    if approval == "wrong_draft":
        quality = quality.model_copy(update={"draft_version": 2})
    elif approval == "wrong_plan":
        quality = quality.model_copy(update={"plan_version": 2})
    original = ["explicit optional degradation", "unclassified historical warning"]
    state = {
        "run_id": "finalizer-regression",
        "query": "回归",
        "plan": plan,
        "plans": [plan],
        "research_results": results,
        "warnings": list(original),
        "operational_warnings": [original[0]],
        "draft_versions": [DraftVersion(version=1, plan_version=1, content=report())],
        "quality_history": [] if approval == "missing" else [quality],
        "requested_final_status": RunStatus.COMPLETED.value,
    }
    final = await workflow._finalizer(state)
    expected = original[1:] if approval == "current" else original
    assert final["final_status"] == RunStatus.COMPLETED_WITH_WARNINGS.value
    assert final["final_warnings"] == expected
    assert state["warnings"] == original  # Checkpoints retain the complete audit trail.
    assert events[-1].data["active_warnings"] == expected
    assert events[-1].data["historical_operational_warnings"] == (
        original[:1] if approval == "current" else []
    )
    assert RunService._artifact_changes({**state, **final})["warnings"] == expected
    assert RunService._artifact_changes(state)["warnings"] == original
