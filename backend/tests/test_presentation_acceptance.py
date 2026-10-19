"""Presentation acceptance keeps facts honest and removes optional rework.

All Runs, models and sources here are disposable offline fixtures.
"""

from __future__ import annotations

import pytest
from atlasflow.agents.contracts import (
    CritiqueRoute,
    QualityDecision,
    QualityRoute,
    ReviewIssue,
    RunPolicy,
    Severity,
)
from atlasflow.agents.gateway import OpenRouterModelGateway, _quality_schema
from atlasflow.agents.report_quality import apply_report_audit
from atlasflow.agents.task_quality import calibrate_task_result, execution_accepted
from atlasflow.bootstrap import _workflow_options
from atlasflow.config import Settings
from atlasflow.schemas import Evidence, RunEventType, RunStatus, ToolCallRecord

from backend.tests.fakes import (
    FakeModelGateway,
    GatewayScenario,
    make_test_container,
    make_test_settings,
)
from backend.tests.test_quality_output_recovery import DRAFT, RESULTS, Client, verdict
from backend.tests.test_task_acceptance import AcceptanceGateway, result, task


def presentation_settings(**updates):
    # Exercise actual production defaults, not the strict legacy regression rig.
    fields = (
        "research_accept_partial_results",
        "research_quorum_ratio",
        "quality_threshold",
        "quality_relaxed_review",
        "max_report_revisions",
    )
    defaults = {name: Settings.model_fields[name].default for name in fields}
    return make_test_settings().model_copy(update={**defaults, **updates})


def decision(score=65, route=QualityRoute.ACCEPT, issues=()):
    return QualityDecision(
        decision=route,
        plan_version=1,
        draft_version=1,
        score=score,
        rationale="核心回答可用，保留真实证据范围",
        issues=list(issues),
        revision_instructions=["仅修正已指出问题"] if route is QualityRoute.REVISE else [],
    )


def issue(code, severity=Severity.WARNING):
    return ReviewIssue(
        severity=severity,
        code=code,
        message="本项存在可定位问题",
        recommendation="按实际问题调整",
        task_id=None,
    )


def test_default_profile_is_presentation_with_ten_forty_tool_budgets():
    settings = presentation_settings()
    assert settings.research_accept_partial_results and settings.quality_relaxed_review
    assert settings.quality_threshold == 50 and settings.max_report_revisions == 1
    assert settings.research_quorum_ratio == 0.5
    options = _workflow_options(settings)
    assert options["accept_partial_results"] and options["quality_relaxed_review"]
    assert (RunPolicy().max_tool_calls_per_turn, RunPolicy().max_tool_calls_per_run) == (10, 40)


@pytest.mark.parametrize("statuses", [("met", "unmet"), ("partial",), ("partial", "unmet")])
def test_usable_partial_retains_reported_reliability_and_original_coverage(statuses):
    current = task(success_criteria=[f"标准{i}" for i in range(len(statuses))])
    raw = result(statuses=statuses)
    strict = calibrate_task_result(current, raw)
    accepted = calibrate_task_result(current, raw, accept_partial_results=True)
    assert accepted.outcome == "partial" and execution_accepted(accepted)
    assert not execution_accepted(strict)
    assert accepted.confidence == raw.confidence >= strict.confidence
    assert accepted.criteria_coverage == strict.criteria_coverage
    assert accepted.assessments == strict.assessments and accepted.limitations
    assert raw.execution_accepted is None  # The model's original is not mutated.


def test_low_confidence_no_longer_blocks_usable_partial_execution():
    accepted = calibrate_task_result(task(), result(confidence=0.35), accept_partial_results=True)
    assert accepted.confidence == 0.35 and accepted.execution_accepted
    assert accepted.outcome == "partial"


def test_zero_coverage_cannot_become_success():
    assessed = calibrate_task_result(
        task(), result(statuses=("unmet",)), accept_partial_results=True
    )
    assert not assessed.execution_accepted and assessed.criteria_coverage == 0


def test_current_claim_without_any_bound_source_cannot_become_success():
    current = task(requires_fresh_data=True, required_capabilities=["web_search"])
    raw = result()
    raw.assessments[0].source_ids = ["invented"]
    assessed = calibrate_task_result(current, raw, accept_partial_results=True)
    assert not assessed.execution_accepted and assessed.assessments[0].source_ids == []


def test_grounded_partial_current_claim_is_accepted_but_does_not_gain_coverage():
    current = task(requires_fresh_data=True, required_capabilities=["web_search"])
    evidence = Evidence(
        id="source-1",
        source_id="web",
        title="官方资料",
        content="已核验的部分事实",
        uri="https://official.example.test/facts",
        score=1,
    )
    raw = result(statuses=("partial",))
    raw.assessments[0].source_ids = [evidence.id]
    assessed = calibrate_task_result(current, raw, evidence=[evidence], accept_partial_results=True)
    assert assessed.execution_accepted and assessed.criteria_coverage == 0.5
    assert assessed.outcome == "partial" and assessed.confidence == raw.confidence


def test_partial_navigation_keeps_real_links_without_lowering_supported_reliability():
    current = task(requires_fresh_data=True, required_capabilities=["map_itinerary"])
    call = ToolCallRecord(
        call_id="map-proof",
        tool_name="map_itinerary",
        arguments={},
        success=False,
        duration_ms=1,
        navigation_urls=["https://uri.amap.com/navigation?known=1"],
    )
    raw = result(statuses=("partial",))
    raw.assessments[0].source_ids = [call.call_id]
    assessed = calibrate_task_result(
        current, raw, records=[call], required_tools_missing=True, accept_partial_results=True
    )
    assert assessed.execution_accepted and assessed.outcome == "partial"
    assert assessed.confidence == raw.confidence and assessed.limitations
    assert call.success is False  # A partially failed tool is never rewritten.


@pytest.mark.asyncio
async def test_partial_run_completes_once_without_warning_or_extra_research():
    gateway = AcceptanceGateway(repair=False)
    container = make_test_container(model=gateway, settings=presentation_settings())
    run = await container.run_service.execute_and_wait("展示研究流程，采纳有用的部分结论")
    assert run.status is RunStatus.COMPLETED and not run.warnings
    assert gateway.research_attempts == {"p1-t1": 1, "p1-t2": 1, "p1-t3": 1}
    assert run.metrics.successful_tasks == 3 and run.metrics.partial_tasks == 0
    assert run.metrics.replan_count == 0 and run.metrics.revision_count == 0
    partial = next(item for item in run.research_results if item.outcome == "partial")
    assert partial.execution_accepted and partial.confidence == 0.9 and partial.limitations
    event = next(
        event
        for event in run.events
        if event.event_type is RunEventType.TASK_COMPLETED and event.task_id == partial.task_id
    )
    assert event.status == "succeeded" and event.data["execution_accepted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [50, 55, 59, 60, 65, 79])
async def test_report_below_old_eighty_threshold_can_complete_without_revision(score):
    gateway = FakeModelGateway(GatewayScenario(quality_scores=[score]))
    container = make_test_container(model=gateway, settings=presentation_settings())
    run = await container.run_service.execute_and_wait("展示可用的简洁报告")
    assert run.status is RunStatus.COMPLETED and not run.warnings
    assert run.quality_history[-1].score == score and run.metrics.revision_count == 0


@pytest.mark.asyncio
async def test_noncritical_supplement_does_not_expand_already_accepted_work():
    gateway = FakeModelGateway(GatewayScenario(critique_routes=[CritiqueRoute.SUPPLEMENT]))
    container = make_test_container(model=gateway, settings=presentation_settings())
    run = await container.run_service.execute_and_wait("展示验收不追加非关键研究")
    assert run.status is RunStatus.COMPLETED and run.metrics.supplement_rounds == 0
    assert run.critique_history[-1].decision is CritiqueRoute.ACCEPT
    assert run.critique_history[-1].issues  # Improvement notes remain auditable.


@pytest.mark.asyncio
async def test_explicitly_requested_supplement_is_still_executed():
    gateway = FakeModelGateway(GatewayScenario(critique_routes=[CritiqueRoute.SUPPLEMENT]))
    container = make_test_container(model=gateway, settings=presentation_settings())
    run = await container.run_service.execute_and_wait(
        "演示指定补充流程",
        policy=RunPolicy(
            initial_task_count=2, required_supplement_rounds=1, supplement_task_count=1
        ),
    )
    assert run.status is RunStatus.COMPLETED and run.metrics.supplement_rounds == 1
    assert len(run.plan.tasks) == 3


@pytest.mark.parametrize(
    "code", ["report_missing_sections", "report_too_brief", "presentation_style",
             "report_limitation_note", "report_optional_detail"]
)
def test_soft_formatting_does_not_force_revision_or_inflate_the_score(code):
    before = decision()
    after = apply_report_audit(before, [issue(code)], threshold=60, relaxed_review=True)
    assert after.decision is QualityRoute.ACCEPT and after.score == before.score
    assert any(item.code == code for item in after.issues)
    strict = apply_report_audit(before, [issue(code)], threshold=60)
    assert strict.decision is QualityRoute.REVISE


@pytest.mark.parametrize(
    "code", ["report_unknown_source", "report_unknown_citation", "report_missing_source"]
)
def test_source_or_citation_problem_still_requires_correction(code):
    after = apply_report_audit(decision(), [issue(code)], threshold=60, relaxed_review=True)
    assert after.decision is QualityRoute.REVISE and after.score == 65


def test_critical_missing_facts_cannot_be_downgraded_to_presentation_success():
    after = apply_report_audit(
        decision(),
        [issue("report_missing_fresh_evidence", Severity.CRITICAL)],
        threshold=60,
        relaxed_review=True,
    )
    assert after.decision is QualityRoute.REPLAN and after.score == 65


def test_style_only_model_revision_can_be_accepted_without_raising_its_score():
    before = decision(route=QualityRoute.REVISE, issues=[issue("presentation_style")])
    after = apply_report_audit(before, [], threshold=60, relaxed_review=True)
    assert after.decision is QualityRoute.ACCEPT and after.score == 65 and after.issues


@pytest.mark.asyncio
async def test_gateway_schema_prompt_and_validator_follow_selected_quality_threshold():
    client = Client([verdict("accept", score=65)])
    gateway = OpenRouterModelGateway(client, "test", quality_threshold=60)
    output = await gateway.evaluate_report("展示验收", DRAFT, RESULTS)
    assert output.score == 65 and output.decision is QualityRoute.ACCEPT
    schema = client.requests[0]["response_format"]["json_schema"]["schema"]
    accept = schema["properties"]["verdict"]["anyOf"][0]
    assert accept["properties"]["score"]["minimum"] == 60
    assert "至少60" in client.requests[0]["messages"][0]["content"]
    assert (
        _quality_schema(80)["properties"]["verdict"]["anyOf"][0]["properties"]["score"]["minimum"]
        == 80
    )


@pytest.mark.asyncio
async def test_explicit_strict_gateway_threshold_still_requires_eighty():
    client = Client([verdict("accept", score=65), verdict("accept", score=85)])
    gateway = OpenRouterModelGateway(client, "test", quality_threshold=80, relaxed_review=False)
    output = await gateway.evaluate_report("严格验收", DRAFT, RESULTS)
    assert output.score == 85 and len(client.requests) == 2


@pytest.mark.asyncio
async def test_critical_critic_gap_is_not_converted_into_optional_acceptance():
    class CriticalSupplement(FakeModelGateway):
        async def review_research(self, *args, **kwargs):
            reviewed = await super().review_research(*args, **kwargs)
            if reviewed.decision is CritiqueRoute.SUPPLEMENT:
                reviewed.issues[0].severity = Severity.CRITICAL
            return reviewed

    gateway = CriticalSupplement(GatewayScenario(critique_routes=[CritiqueRoute.SUPPLEMENT]))
    container = make_test_container(model=gateway, settings=presentation_settings())
    run = await container.run_service.execute_and_wait("核心事实缺口仍需要补充")
    assert run.status is RunStatus.COMPLETED and run.metrics.supplement_rounds == 1
    assert run.critique_history[0].decision is CritiqueRoute.SUPPLEMENT


@pytest.mark.asyncio
async def test_at_most_one_substantive_revision_with_presentation_defaults():
    gateway = FakeModelGateway(
        GatewayScenario(quality_routes=[QualityRoute.REVISE, QualityRoute.REVISE])
    )
    container = make_test_container(model=gateway, settings=presentation_settings())
    run = await container.run_service.execute_and_wait("展示修订严格有界")
    assert gateway.call_counts["revise_report"] == 1 and gateway.call_counts["evaluate_report"] == 2
    assert run.metrics.revision_count == 1 and run.status is RunStatus.COMPLETED_WITH_WARNINGS


@pytest.mark.asyncio
async def test_real_missing_task_is_not_silently_hidden_by_partial_acceptance():
    gateway = FakeModelGateway(GatewayScenario(permanent_research_failures={"p1-t1"}))
    container = make_test_container(model=gateway, settings=presentation_settings())
    run = await container.run_service.execute_and_wait("没有任何结果的任务不是部分成功")
    assert run.metrics.failed_tasks == 1 and run.status is RunStatus.COMPLETED_WITH_WARNINGS
    assert any("p1-t1" in warning for warning in run.warnings)
