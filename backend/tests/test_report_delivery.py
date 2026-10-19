"""Report-first delivery is bounded and never manufactures factual acceptance."""

import pytest
from atlasflow.agents.contracts import (
    CritiqueRoute,
    QualityRoute,
    ResearchPlan,
    RunPolicy,
    Severity,
)
from atlasflow.agents.report_delivery import build_delivery_report
from atlasflow.agents.report_quality import audit_report, limit_to_report_repair
from atlasflow.agents.report_skill import (
    build_citation_catalog,
    build_report_context,
    render_academic_references,
)
from atlasflow.agents.task_quality import calibrate_task_result
from atlasflow.agents.tool_runtime import ToolStageResult
from atlasflow.providers.openrouter import ProviderRequestError
from atlasflow.schemas import Evidence, RunStatus, ToolCallRecord

from backend.tests.fakes import FakeModelGateway, GatewayScenario, make_test_container
from backend.tests.test_execution_recovery import harness
from backend.tests.test_presentation_acceptance import decision, issue, presentation_settings
from backend.tests.test_task_acceptance import result, task


def source(key="s1", **updates):
    return Evidence(
        id=key,
        source_id=key,
        title="已获取的资料",
        content="可核验的原始材料",
        uri=updates.pop("uri", f"https://example.test/{key}"),
        **updates,
    )


@pytest.mark.parametrize("statuses", [("partial",), ("met", "unmet"), ("met", "partial")])
def test_supported_partial_confidence_is_not_a_completion_percentage(statuses):
    current = task(success_criteria=[f"要求{i}" for i in range(len(statuses))])
    raw = result(statuses=statuses, confidence=0.9)
    assessed = calibrate_task_result(current, raw, accept_partial_results=True)
    strict = calibrate_task_result(current, raw)
    assert assessed.confidence == assessed.reported_confidence == 0.9
    assert strict.confidence < assessed.confidence
    assert assessed.criteria_coverage == strict.criteria_coverage < 1
    assert assessed.outcome == "partial" and assessed.execution_accepted


def test_partial_confidence_does_not_gain_a_fake_floor_or_reopen_historical_scores():
    current = task()
    low = calibrate_task_result(
        current, result(statuses=("partial",), confidence=0.25), accept_partial_results=True
    )
    assert low.confidence == 0.25
    old = result(statuses=("partial",), confidence=0.5, reported_confidence=0.9)
    assert calibrate_task_result(current, old, accept_partial_results=True).confidence == 0.5


def test_unbound_fresh_claim_stays_low_and_unaccepted():
    current = task(requires_fresh_data=True, required_capabilities=["web_search"])
    raw = result(confidence=0.95)
    raw.assessments[0].source_ids = ["made-up-id"]
    assessed = calibrate_task_result(current, raw, accept_partial_results=True)
    assert assessed.confidence == 0.5 and not assessed.execution_accepted
    assert assessed.outcome == "partial" and not assessed.assessments[0].source_ids


def test_new_reviewer_web_source_cannot_renumber_existing_navigation():
    evidence = [source()]
    call = ToolCallRecord(
        call_id="nav",
        tool_name="map_route",
        arguments={},
        success=True,
        duration_ms=1,
        navigation_urls=["https://uri.amap.com/navigation?from=a&to=b"],
    )
    initial = build_citation_catalog(evidence, [call])
    order = [item.url for item in initial]
    new_evidence = [*evidence, source("review")]
    catalog = build_citation_catalog(new_evidence, [call], source_order=order)
    assert [item.url for item in catalog[:2]] == order
    assert catalog[1].kind == "navigation" and catalog[2].url.endswith("/review")
    context = build_report_context(new_evidence, [call], source_order=order)
    assert "[2] 高德地图导航" in context and "[3] 已获取的资料" in context
    body = "## 摘要\n\n导航见[2]。\n\n## 结论\n\n保留链接。\n\n## 局限\n\n出发前核实。"
    report = render_academic_references(body, new_evidence, [call], source_order=order)
    assert "[2] 高德地图导航" in report and "[3] 已获取的资料" not in report
    plan = ResearchPlan(plan_version=1, rationale="测试", tasks=[task(), task("T2")])
    assert not audit_report(report, plan, [result()], new_evidence, [call], source_order=order)


def test_quality_replan_becomes_local_repair_without_faking_score_or_severity():
    before = decision(
        score=40,
        route=QualityRoute.REPLAN,
        issues=[issue("factual_contradiction", Severity.CRITICAL)],
    )
    after = limit_to_report_repair(before).validate_for_execution()
    assert after.decision is QualityRoute.REVISE and after.score == 40
    assert after.issues == before.issues and after.issues[0].severity is Severity.CRITICAL
    assert after.revision_instructions and before.decision is QualityRoute.REPLAN


@pytest.mark.asyncio
async def test_local_quality_problem_is_revised_once_without_replanning():
    gateway = FakeModelGateway(
        GatewayScenario(quality_routes=[QualityRoute.REPLAN, QualityRoute.ACCEPT])
    )
    run = await make_test_container(
        model=gateway, settings=presentation_settings()
    ).run_service.execute_and_wait("保留主线并修正引用")
    assert run.status is RunStatus.COMPLETED and run.report
    assert gateway.call_counts["create_plan"] == gateway.call_counts["revise_report"] == 1
    assert gateway.call_counts["evaluate_report"] == 2 and run.metrics.replan_count == 0
    assert run.quality_history[0].score == 40
    assert run.quality_history[0].issues[0].severity is Severity.CRITICAL
    assert run.quality_history[0].decision is QualityRoute.REVISE


@pytest.mark.asyncio
async def test_persistent_rejection_delivers_annotated_full_report_not_false_acceptance():
    gateway = FakeModelGateway(
        GatewayScenario(quality_routes=[QualityRoute.REPLAN, QualityRoute.REPLAN])
    )
    run = await make_test_container(
        model=gateway, settings=presentation_settings()
    ).run_service.execute_and_wait("有界交付")
    assert run.status is RunStatus.COMPLETED_WITH_WARNINGS and run.report and not run.error
    assert run.metrics.revision_count == 1 and run.metrics.replan_count == 0
    assert all(item.decision is QualityRoute.REVISE for item in run.quality_history)
    assert run.quality_history[-1].score == 40
    assert len(run.draft_versions) == 3 and "未获得完整质量验收" in run.report
    assert "> 修订后的结论成立" in run.report  # Retained only as explicitly unverified quoted text.
    assert "### 待核实事项" in run.report and "### 核验说明" in run.report
    assert "修订后的结论成立" not in run.report.split("\n\n## 局限\n\n")[0]


class BrokenProvider(FakeModelGateway):
    def __init__(self, method, *, programming_error=False):
        super().__init__(
            GatewayScenario(
                quality_routes=[QualityRoute.REVISE] if method == "revise_report" else []
            )
        )
        self.method = method
        self.programming_error = programming_error

    def _maybe_fail(self, method):
        if method == self.method:
            if self.programming_error:
                raise RuntimeError("fixture programming defect")
            raise ProviderRequestError("fixture provider unavailable or output invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["create_plan", "review_research", "synthesize_report", "evaluate_report", "revise_report"],
)
async def test_provider_failure_still_delivers_an_explicitly_limited_report(method):
    gateway = BrokenProvider(method)
    run = await make_test_container(
        model=gateway, settings=presentation_settings()
    ).run_service.execute_and_wait("服务异常仍需有限范围交付")
    assert run.status is RunStatus.COMPLETED_WITH_WARNINGS and run.report and not run.error
    assert "未获得完整质量验收" in run.report and run.warnings and run.errors
    assert any(item.code.endswith("_delivery_fallback") for item in run.errors)
    assert not any(item.decision is QualityRoute.ACCEPT for item in run.quality_history)
    assert run.report == run.draft_versions[-1].content
    assert gateway.call_counts[method] == 1  # No extra hidden retries/model calls.


@pytest.mark.asyncio
async def test_provider_fallback_does_not_claim_to_answer_when_planning_never_succeeded():
    gateway = BrokenProvider("create_plan")
    run = await make_test_container(
        model=gateway, settings=presentation_settings()
    ).run_service.execute_and_wait("明天的实时门票")
    assert "现有材料不足以形成可靠的最终结论" in run.report
    assert not run.research_results and run.metrics.total_tasks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("programming_error", [False, True])
async def test_strict_mode_or_programming_error_is_not_converted_to_delivery(programming_error):
    gateway = BrokenProvider("evaluate_report", programming_error=programming_error)
    settings = presentation_settings(research_accept_partial_results=programming_error)
    run = await make_test_container(model=gateway, settings=settings).run_service.execute_and_wait(
        "不可吞掉程序缺陷"
    )
    assert run.status is RunStatus.FAILED and run.error
    assert not any(item.code.endswith("_delivery_fallback") for item in run.errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [{"p1-t1", "p1-t2"}, {"p1-t1", "p1-t2", "p1-t3"}])
async def test_quorum_failure_delivers_available_work_without_starting_a_new_plan(failed):
    gateway = FakeModelGateway(GatewayScenario(permanent_research_failures=failed))
    run = await make_test_container(
        model=gateway, settings=presentation_settings()
    ).run_service.execute_and_wait("已有结果必须交付")
    assert run.status is RunStatus.COMPLETED_WITH_WARNINGS and run.report
    assert gateway.call_counts["create_plan"] == 1 and run.metrics.replan_count == 0
    assert run.metrics.failed_tasks == len(failed)
    assert not run.quality_history or run.quality_history[-1].decision is QualityRoute.ACCEPT


@pytest.mark.asyncio
async def test_critic_replan_keeps_known_issue_but_does_not_restart_research():
    class Observed(FakeModelGateway):
        async def synthesize_report(self, *args, **kwargs):
            assert "invalid_decomposition" in kwargs["tool_context"]
            return await super().synthesize_report(*args, **kwargs)

    gateway = Observed(GatewayScenario(critique_routes=[CritiqueRoute.REPLAN]))
    run = await make_test_container(
        model=gateway, settings=presentation_settings()
    ).run_service.execute_and_wait("保留证据范围")
    assert run.report and run.status is RunStatus.COMPLETED_WITH_WARNINGS
    assert run.critique_history[-1].decision is CritiqueRoute.REPLAN
    assert gateway.call_counts["create_plan"] == 1 and run.metrics.replan_count == 0


def test_fallback_rebuilds_real_citations_and_drops_unknown_links_and_numbers():
    current = task(requires_fresh_data=True, required_capabilities=["web_search"])
    evidence = source()
    raw = result()
    raw.findings = ["部分事实见[99]和[来源](https://invented.test/nope)，已有实际来源。"]
    raw.assessments[0].source_ids = [evidence.id]
    plan = ResearchPlan(plan_version=1, rationale="测试", tasks=[current, task("T2")])
    draft = build_delivery_report("当前事实", plan, [raw], [evidence], [], reason="审查服务不可用")
    assert "[99]" not in draft.content and "invented.test" not in draft.content
    assert "[1] 已获取的资料" in draft.content and "部分事实" in draft.content
    assert draft.citation_source_urls == [evidence.uri]
    assert not audit_report(draft.content, plan, [raw], [evidence], [])


def test_fallback_withholds_ungrounded_fresh_findings_and_known_critical_task():
    current = task(requires_fresh_data=True, required_capabilities=["web_search"])
    plan = ResearchPlan(plan_version=1, rationale="测试", tasks=[current, task("T2")])
    raw = result()
    raw.findings = ["不应发布的实时结论"]
    draft = build_delivery_report("问题", plan, [raw], [], [], reason="未能核验")
    assert "不应发布的实时结论" not in draft.content
    grounded = source()
    raw.assessments[0].source_ids = [grounded.id]
    problem = issue("contradiction", Severity.CRITICAL).model_copy(
        update={"task_id": current.task_id}
    )
    draft = build_delivery_report(
        "问题", plan, [raw], [grounded], [], reason="待修正", issues=[problem]
    )
    assert "不应发布的实时结论" not in draft.content
    assert draft.based_on_task_ids == []


@pytest.mark.asyncio
async def test_quality_revision_uses_existing_evidence_instead_of_repeating_tools():
    gateway = FakeModelGateway(
        GatewayScenario(quality_routes=[QualityRoute.REPLAN, QualityRoute.ACCEPT])
    )
    container = make_test_container(model=gateway, settings=presentation_settings())
    workflow = container.run_service.workflow
    original = workflow._tool_stage
    reviews = []

    async def stage(state, agent, **kwargs):
        if agent == "quality_gate":
            reviews.append(len(state.get("draft_versions", [])))
            return ToolStageResult()
        return await original(state, agent, **kwargs)

    workflow._tool_stage = stage
    run = await container.run_service.execute_and_wait("局部修订不重新搜集一轮")
    assert run.status is RunStatus.COMPLETED and reviews == [1]


@pytest.mark.asyncio
async def test_full_revision_keeps_citation_urls_across_new_review_evidence():
    navigation = "https://uri.amap.com/navigation?from=a&to=b"

    class Observed(FakeModelGateway):
        async def synthesize_report(self, *args, **kwargs):
            assert "[2] 高德地图导航" in kwargs["tool_context"]
            draft = await super().synthesize_report(*args, **kwargs)
            return draft.model_copy(
                update={"content": draft.content.replace("测试结论成立", "导航见[2]，测试结论成立")}
            )

        async def evaluate_report(self, *args, **kwargs):
            assert "[2] 高德地图导航" in kwargs["tool_context"]
            assert args[1].citation_source_urls[:2] == [source().uri, navigation]
            return await super().evaluate_report(*args, **kwargs)

        async def revise_report(self, *args, **kwargs):
            assert "[2] 高德地图导航" in kwargs["tool_context"]
            assert "[3] 已获取的资料" in kwargs["tool_context"]
            draft = await super().revise_report(*args, **kwargs)
            return draft.model_copy(
                update={
                    "content": draft.content.replace(
                        "修订后的结论成立", "导航见[2]，补充依据见[3]，修订后的结论成立"
                    )
                }
            )

    gateway = Observed(GatewayScenario(quality_routes=[QualityRoute.REPLAN, QualityRoute.ACCEPT]))
    container = make_test_container(model=gateway, settings=presentation_settings())

    async def stage(state, agent, **kwargs):
        if agent == "synthesizer" and not state.get("draft_versions"):
            return ToolStageResult(
                evidence=[source()],
                records=[
                    ToolCallRecord(
                        call_id="map",
                        tool_name="map_route",
                        arguments={},
                        success=True,
                        duration_ms=1,
                        navigation_urls=[navigation],
                    )
                ],
            )
        if agent == "quality_gate":
            return ToolStageResult(evidence=[source("review")])
        return ToolStageResult()

    container.run_service.workflow._tool_stage = stage
    run = await container.run_service.execute_and_wait("引用顺序回归")
    assert run.status is RunStatus.COMPLETED and "[2] 高德地图导航" in run.report
    assert "[3] 已获取的资料" in run.report
    assert run.draft_versions[0].citation_source_urls == [source().uri, navigation]
    assert run.draft_versions[1].citation_source_urls == [
        source().uri,
        navigation,
        source("review").uri,
    ]


@pytest.mark.asyncio
async def test_limited_delivery_and_diagnostics_survive_persistent_reconciliation(tmp_path):
    gateway = BrokenProvider("evaluate_report")
    runtime, store, _workflow, service, _ = await harness(tmp_path, gateway=gateway)
    try:
        run = await service.execute_and_wait("持久化有限范围交付")
        assert run.status is RunStatus.COMPLETED_WITH_WARNINGS and run.report
        calls = gateway.call_counts.copy()
        await service.reconcile_startup()
        restored = await store.get(run.id)
        assert restored.report == run.report and gateway.call_counts == calls
        assert restored.draft_versions[-1].content == run.report
        assert any(item.code == "quality_gate_delivery_fallback" for item in restored.errors)
        bundle = await runtime.repository.read(run.id)
        assert all(item["delivered"] for item in bundle.outbox.values())
    finally:
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_user_cancellation_is_never_changed_into_success():
    container = make_test_container(settings=presentation_settings())
    workflow = container.run_service.workflow
    run = await container.store.create("取消不能继续交付")
    state = workflow._initial_state(
        run_id=run.id, query=run.query, auto_approve=True, policy=RunPolicy()
    )
    state.update(
        requested_final_status=RunStatus.CANCELLED.value, delivery_fallback_reason="模型服务故障"
    )
    output = await workflow._finalizer(state)
    assert output["final_status"] == RunStatus.CANCELLED.value and output["report"] is None
    assert "draft_versions" not in output
