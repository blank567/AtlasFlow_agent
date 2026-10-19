"""Task correctness must improve through acceptance/evidence, never score inflation."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from atlasflow.agents.contracts import (
    AgentError,
    CriterionAssessment,
    CritiqueDecision,
    CritiqueRoute,
    ReplanReason,
    ResearchPlan,
    ResearchResult,
    ResearchTask,
    ReviewContext,
    ReviewIssue,
    RunPolicy,
    Severity,
)
from atlasflow.agents.gateway import OpenRouterModelGateway
from atlasflow.agents.task_quality import (
    calibrate_task_result,
    repaired_task_ids,
    validate_local_repairs,
    validate_task_result,
)
from atlasflow.agents.tool_runtime import RequiredToolError, ToolStageResult
from atlasflow.schemas import Evidence, RunEventType, RunStatus, ToolCallRecord
from atlasflow.tools.calculator import CalculatorTool
from atlasflow.tools.map_itinerary import MapItineraryTool
from atlasflow.tools.registry import ToolRegistry

from backend.tests.fakes import FakeModelGateway, GatewayScenario, make_test_container
from backend.tests.test_capability_planning import WeatherTool
from backend.tests.test_tool_runtime import _call, _response, _runtime


async def ignore_event(_run_id, _event):
    return None


def task(task_id="T1", **updates):
    return ResearchTask(
        task_id=task_id,
        title=f"任务{task_id}",
        objective="给出可验证的结论",
        success_criteria=updates.pop("success_criteria", ["形成可验证结论"]),
        priority=1,
        plan_version=1,
        **updates,
    )


def result(task_id="T1", statuses=("met",), **updates):
    return ResearchResult(
        task_id=task_id,
        plan_version=1,
        summary="已取得可验证的结果",
        findings=["可验证发现"],
        confidence=updates.pop("confidence", 0.95),
        assessments=[
            CriterionAssessment(
                criterion_index=index,
                status=status,
                finding_indices=[1] if status != "unmet" else [],
                reason="有对应的发现" if status == "met" else "仍缺少验收所需信息",
            )
            for index, status in enumerate(statuses, 1)
        ],
        **updates,
    )


@pytest.mark.parametrize("placeholder", [",", "...", "，。", "\n\t", "—"])
def test_new_results_reject_punctuation_but_historical_record_can_still_load(placeholder):
    old = result().model_dump()
    old["findings"] = [placeholder]
    # Historical whitespace-only data was already invalid; punctuation remains
    # readable. Test execution validation without breaking old-record parsing.
    if placeholder.strip():
        loaded = ResearchResult.model_validate(old)
        with pytest.raises(ValueError, match="substantive"):
            validate_task_result(task(), loaded)
    else:
        with pytest.raises(ValueError):
            ResearchResult.model_validate(old)


@pytest.mark.parametrize(
    "assessments",
    [
        [],
        [{"criterion_index": 2, "status": "met", "finding_indices": [1], "reason": "对应事实"}],
        [{"criterion_index": 1, "status": "met", "finding_indices": [], "reason": "对应事实"}],
        [{"criterion_index": 1, "status": "met", "finding_indices": [2], "reason": "对应事实"}],
        [{"criterion_index": 1, "status": "met", "finding_indices": [1, 1], "reason": "对应事实"}],
    ],
)
def test_fresh_acceptance_requires_exact_criterion_and_finding_mapping(assessments):
    value = result().model_copy(
        update={"assessments": [CriterionAssessment(**item) for item in assessments]}
    )
    with pytest.raises(ValueError):
        validate_task_result(task(), value, require_assessments=True)


def test_partial_acceptance_caps_confidence_and_does_not_raise_a_conservative_score():
    current = task(success_criteria=["回答第一点", "回答第二点"])
    assessed = calibrate_task_result(current, result(statuses=("met", "unmet")))
    assert assessed.outcome == "partial" and assessed.criteria_coverage == 0.5
    assert assessed.reported_confidence == 0.95 and assessed.confidence == 0.5
    assert assessed.limitations
    conservative = calibrate_task_result(task(), result(confidence=0.35))
    assert conservative.outcome == "partial" and conservative.confidence == 0.35


def test_fresh_claim_requires_real_acquired_source_not_an_invented_id():
    current = task(requires_fresh_data=True, required_capabilities=["web_search"])
    evidence = Evidence(
        id="source-1",
        source_id="web",
        title="官方来源",
        content="本次核查事实",
        uri="https://official.example.test/facts",
        score=1,
    )
    raw = result()
    raw.assessments[0].source_ids = ["invented-source"]
    assessed = calibrate_task_result(current, raw, evidence=[evidence])
    assert assessed.outcome == "partial" and assessed.confidence == 0.5
    assert assessed.assessments[0].source_ids == []
    raw.assessments[0].source_ids = ["source-1"]
    verified = calibrate_task_result(current, raw, evidence=[evidence])
    assert verified.outcome == "complete" and verified.confidence == 0.95


def test_successful_navigation_call_id_is_a_compact_grounding_anchor():
    current = task(requires_fresh_data=True, required_capabilities=["map_itinerary"])
    call = ToolCallRecord(
        call_id="map-proof",
        tool_name="map_itinerary",
        arguments={},
        success=True,
        duration_ms=1,
        navigation_urls=["https://uri.amap.com/navigation?known=1"],
    )
    raw = result()
    raw.assessments[0].source_ids = [call.call_id]
    assert calibrate_task_result(current, raw, records=[call]).outcome == "complete"
    partial = calibrate_task_result(current, raw, records=[call], required_tools_missing=True)
    assert partial.outcome == "partial" and partial.confidence <= 0.49


def test_success_summary_without_a_source_cannot_ground_current_facts():
    current = task(requires_fresh_data=True, required_capabilities=["web_search"])
    call = ToolCallRecord(
        call_id="no-source",
        tool_name="web_search",
        arguments={},
        success=True,
        duration_ms=1,
        summary="查询完成，但没有来源",
    )
    raw = result()
    raw.assessments[0].source_ids = [call.call_id]
    assessed = calibrate_task_result(current, raw, records=[call])
    assert assessed.outcome == "partial" and assessed.assessments[0].source_ids == []


class AcceptanceGateway(FakeModelGateway):
    def __init__(self, *, repair=True, fail_second=False):
        super().__init__(GatewayScenario(initial_task_count=3, research_delay_seconds=0))
        self.repair = repair
        self.fail_second = fail_second
        self.repair_contexts = []

    async def analyze_task(self, query, current, dependency_results=(), *, tool_context=""):
        original = await super().analyze_task(
            query, current, dependency_results, tool_context=tool_context
        )
        self.repair_contexts.append(tool_context)
        attempt = self.research_attempts[current.task_id]
        if current.task_id == "p1-t1" and attempt == 2 and self.fail_second:
            raise RuntimeError("second analysis unavailable")
        partial = current.task_id == "p1-t1" and (attempt == 1 or not self.repair)
        return original.model_copy(
            update={"assessments": result(statuses=("partial" if partial else "met",)).assessments}
        )


@pytest.mark.asyncio
async def test_partial_task_is_locally_repaired_without_replanning_or_score_inflation():
    gateway = AcceptanceGateway()
    run = await make_test_container(model=gateway).run_service.execute_and_wait(
        "验证任务的局部修复流程"
    )
    assert run.status is RunStatus.COMPLETED and run.metrics.replan_count == 0
    assert gateway.research_attempts == {"p1-t1": 2, "p1-t2": 1, "p1-t3": 1}
    assert all(item.outcome == "complete" for item in run.research_results)
    assert run.metrics.successful_tasks == 3 and run.metrics.partial_tasks == 0
    assert any("不要为了过关" in context for context in gateway.repair_contexts)
    assert all(item.confidence == 0.9 for item in run.research_results)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_second", [False, True])
async def test_still_partial_task_preserves_usable_work_and_is_not_green_success(fail_second):
    gateway = AcceptanceGateway(repair=False, fail_second=fail_second)
    run = await make_test_container(model=gateway).run_service.execute_and_wait(
        "验证有限修复后的部分研究"
    )
    assert run.status is RunStatus.COMPLETED_WITH_WARNINGS and run.report
    assert (
        run.metrics.partial_tasks == 1
        and run.metrics.successful_tasks == 2
        and run.metrics.failed_tasks == 0
    )
    partial = next(item for item in run.research_results if item.task_id == "p1-t1")
    assert partial.outcome == "partial" and partial.confidence == 0.5
    final = next(
        event
        for event in reversed(run.events)
        if event.event_type is RunEventType.TASK_COMPLETED and event.task_id == "p1-t1"
    )
    assert final.status == "partial" and run.review_contexts[-1].partial_task_ids == ["p1-t1"]
    assert all("p1-t1" not in context.completed_task_ids for context in run.review_contexts)
    assert run.warnings and run.metrics.replan_count == 0


def review_context(**updates):
    return ReviewContext(
        review_round=1,
        plan_version=1,
        plan_revision=0,
        initial_task_ids=["T1", "T2", "T3"],
        current_task_count=3,
        expected_task_count=3,
        completed_task_ids=["T1"],
        failed_task_ids=["T2"],
        supplement_rounds_used=0,
        supplement_rounds_remaining=1,
        **updates,
    )


def repair_decision(repair):
    return CritiqueDecision(
        decision=CritiqueRoute.SUPPLEMENT,
        plan_version=1,
        rationale="局部修复导航",
        issues=[
            ReviewIssue(
                severity=Severity.CRITICAL,
                code="navigation_gap",
                task_id="T2",
                message="缺少导航",
                recommendation="只修复导航缺口",
            )
        ],
        supplemental_tasks=[repair],
    )


@pytest.mark.parametrize(
    "kind", ["missing_target", "lost_navigation", "weaker_criteria", "failed_dependency"]
)
def test_supplements_cannot_ignore_or_weaken_critical_navigation_gap(kind):
    plan = ResearchPlan(
        plan_version=1,
        rationale="三个必要维度",
        tasks=[
            task(),
            task("T2", dependencies=["T1"], required_capabilities=["map_itinerary"]),
            task("T3"),
        ],
    )
    options = {"repairs_task_ids": ["T2"], "required_capabilities": ["map_itinerary"]}
    if kind == "missing_target":
        options["repairs_task_ids"] = []
    elif kind == "lost_navigation":
        options["required_capabilities"] = ["web_search"]
    elif kind == "weaker_criteria":
        options["success_criteria"] = ["仅给出待查询方法"]
    else:
        options["dependencies"] = ["T2"]
        options["repairs_task_ids"] = []
    with pytest.raises(ValueError):
        validate_local_repairs(repair_decision(task("T4", **options)), plan, review_context())


def test_only_complete_criterion_preserving_repair_can_close_a_gap():
    plan = ResearchPlan(
        plan_version=1,
        rationale="明确修复责任",
        tasks=[task(), task("T2"), task("T3", repairs_task_ids=["T2"])],
    )
    partial = result("T3", outcome="partial")
    complete = result("T3", outcome="complete")
    assert repaired_task_ids(plan, [partial]) == set()
    assert repaired_task_ids(plan, [complete]) == {"T2"}
    weakened = plan.model_copy(
        update={
            "tasks": [
                plan.tasks[0],
                plan.tasks[1],
                task("T3", repairs_task_ids=["T2"], success_criteria=["缩减标准"]),
            ]
        }
    )
    assert repaired_task_ids(weakened, [complete]) == set()


def test_isolated_failed_tool_is_not_a_dependency_dead_end():
    decision = CritiqueDecision(
        decision=CritiqueRoute.REPLAN,
        plan_version=1,
        rationale="工具失败",
        replan_reason=ReplanReason.DEPENDENCY_DEAD_END,
        issues=[
            ReviewIssue(
                severity=Severity.CRITICAL,
                code="dependency_dead_end",
                message="任务失败",
                recommendation="局部修复",
            )
        ],
    )
    with pytest.raises(ValueError, match="actual blocked tasks"):
        decision.validate_for(review_context())
    decision.validate_for(review_context(blocked_task_ids=["T3"]))


@pytest.mark.asyncio
async def test_repaired_upstream_can_unblock_original_downstream_with_bound_dependency_id():
    workflow = make_test_container().run_service.workflow
    workflow.event_sink = ignore_event
    plan = ResearchPlan(
        plan_version=1,
        rationale="先修复再恢复依赖",
        tasks=[
            task(),
            task("T2", dependencies=["T1"]),
            task("T3", dependencies=["T2"]),
            task("T4", dependencies=["T1"], repairs_task_ids=["T2"]),
        ],
    )
    state = {
        "run_id": "dependency-repair",
        "query": "验证修复",
        "policy": RunPolicy(),
        "plan": plan,
        "research_results": [result(outcome="complete"), result("T4", outcome="complete")],
        "errors": [
            AgentError(
                agent="researcher",
                node="researcher",
                code="research_failed",
                task_id="T2",
                plan_version=1,
                message="地点待核实",
            )
        ],
        "tool_calls": [],
        "evidence": [],
    }
    scheduled = await workflow._schedule_wave(state)
    assert [item.task_id for item in scheduled["ready_tasks"]] == ["T3"]
    branches = workflow._dispatch_wave({**state, **scheduled})
    assert branches[0].arg["dependency_results"][0].task_id == "T2"
    context = workflow._review_context(state)
    assert context.repaired_task_ids == ["T2"] and context.blocked_task_ids == []
    assert all("任务T2" not in warning for warning in workflow._task_gap_warnings(state))


@pytest.mark.asyncio
async def test_itinerary_writer_does_not_receive_navigation_owned_by_another_task():
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    registry.register(
        MapItineraryTool(
            "test", transport=httpx.MockTransport(lambda request: pytest.fail("must not query map"))
        )
    )
    requests = []

    def decide(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert {item["function"]["name"] for item in payload["tools"]} == {"calculator"}
        return _response(
            _call("calculator", {"expression": "1+1"})
            if len(requests) == 1
            else {"content": "done"}
        )

    runtime, _ = _runtime(registry, decide)
    workflow = make_test_container().run_service.workflow
    workflow.tool_runtime = runtime
    writer = task(required_capabilities=["calculator"])
    plan = ResearchPlan(
        plan_version=1,
        rationale="导航独立调度",
        tasks=[writer, task("T2", dependencies=["T1"], required_capabilities=["map_itinerary"])],
    )
    stage = await workflow._tool_stage(
        {
            "run_id": "ownership",
            "query": "规划行程",
            "task": writer,
            "plan": plan,
            "policy": RunPolicy(),
        },
        "researcher",
        focus="编排行程",
        task_id="T1",
        plan_version=1,
    )
    assert stage.records[0].tool_name == "calculator"


@pytest.mark.asyncio
@pytest.mark.parametrize("age,reusable", [(0, True), (301, False)])
async def test_local_retry_can_reuse_only_recent_same_task_fresh_acquisition(age, reusable):
    registry = ToolRegistry()
    registry.register(WeatherTool())
    runtime, _ = _runtime(registry, lambda request: _response({"content": "done"}))
    prior = ToolCallRecord(
        tool_name="weather_provider_a",
        agent="researcher",
        task_id="T1",
        plan_version=1,
        capabilities=["weather_lookup"],
        arguments={},
        success=True,
        duration_ms=1,
        created_at=datetime.now(UTC) - timedelta(seconds=age),
        summary="本轮已获取的天气",
    )
    args = {
        "run_id": "same-run",
        "agent": "researcher",
        "prompt": "只修复结论缺口",
        "policy": RunPolicy(),
        "prior_calls": [prior],
        "required_capabilities": ["weather_lookup"],
        "requires_fresh_data": True,
        "reuse_task_acquisitions": True,
        "task_id": "T1",
        "plan_version": 1,
    }
    if reusable:
        assert not (await runtime.gather(**args)).records
    else:
        with pytest.raises(RequiredToolError):
            await runtime.gather(**args)


@pytest.mark.asyncio
async def test_partial_required_tool_result_is_analyzed_but_never_accepted_complete():
    gateway = AcceptanceGateway()
    workflow = make_test_container(model=gateway).run_service.workflow
    workflow.event_sink = ignore_event
    workflow.tool_runtime, _ = _runtime(
        ToolRegistry(), lambda request: pytest.fail("no provider request")
    )
    current = task("p1-t1")
    record = ToolCallRecord(
        tool_name="map_itinerary",
        arguments={},
        success=False,
        duration_ms=1,
        navigation_urls=["https://uri.amap.com/navigation?partial=1"],
        retryable=False,
    )

    async def partial_stage(*args, **kwargs):
        raise RequiredToolError("部分路段未核实", ToolStageResult(records=[record]))

    workflow._tool_stage = partial_stage
    update = await workflow._researcher(
        {
            "run_id": "partial-stage",
            "query": "核验导航",
            "task": current,
            "plan": ResearchPlan(plan_version=1, rationale="两个任务", tasks=[current, task("T2")]),
            "policy": RunPolicy(),
        }
    )
    assert update["research_results"][0].outcome == "partial"
    assert update["research_results"][0].confidence <= 0.49
    assert len(update["tool_calls"]) == 1 and gateway.research_attempts["p1-t1"] == 1


@pytest.mark.asyncio
async def test_production_gateway_repairs_placeholder_with_field_specific_feedback():
    requests = []

    class Client:
        async def chat(self, **kwargs):
            requests.append(kwargs)
            return {
                "content": json.dumps(
                    {
                        "summary": "计算结果为2",
                        "findings": ["," if len(requests) == 1 else "1+1=2"],
                        "limitations": [],
                        "confidence": 0.95,
                        "assessments": [
                            {
                                "criterion_index": 1,
                                "status": "met",
                                "finding_indices": [1],
                                "source_ids": [],
                                "reason": "直接按基础加法得到",
                            }
                        ],
                    }
                )
            }

    gateway = OpenRouterModelGateway(Client(), "test/model")
    value = await gateway.analyze_task("1+1=?", task())
    assert value.findings == ["1+1=2"] and len(requests) == 2
    assert requests[0]["reasoning_effort"] == "none"
    assert "punctuation" in str(requests[1]["messages"][-1])


@pytest.mark.asyncio
async def test_navigation_failure_supplement_repair_accept_does_not_restart_the_run():
    class RepairGateway(FakeModelGateway):
        def __init__(self):
            super().__init__(GatewayScenario(initial_task_count=3, research_delay_seconds=0))

        async def create_plan(self, *args, **kwargs):
            plan = await super().create_plan(*args, **kwargs)
            plan.tasks[1] = plan.tasks[1].model_copy(
                update={
                    "required_capabilities": ["map_itinerary"],
                    "requires_fresh_data": True,
                    "dependencies": ["p1-t1"],
                }
            )
            return plan

        async def analyze_task(self, query, current, dependency_results=(), *, tool_context=""):
            value = await super().analyze_task(
                query, current, dependency_results, tool_context=tool_context
            )
            assessment = result(current.task_id).assessments
            if current.task_id == "p1-t4":
                assessment[0].source_ids = ["map-proof"]
                assert "待修复任务" in tool_context and "地点条件不匹配" in tool_context
            return value.model_copy(update={"assessments": assessment})

        async def review_research(self, query, plan, results, *, context, **kwargs):
            self.review_contexts.append(context.model_copy(deep=True))
            if context.plan_revision == 0:
                original = plan.task_map["p1-t2"]
                repair = original.model_copy(
                    update={"task_id": "p1-t4", "repairs_task_ids": [original.task_id]}
                )
                return CritiqueDecision(
                    decision=CritiqueRoute.SUPPLEMENT,
                    plan_version=1,
                    rationale="仅修复导航，不重新研究其他已验证维度",
                    supplemental_tasks=[repair],
                    issues=[
                        ReviewIssue(
                            severity=Severity.CRITICAL,
                            code="navigation_gap",
                            task_id=original.task_id,
                            message="导航未核实",
                            recommendation="根据已有地点条件修复",
                        )
                    ],
                )
            assert context.repaired_task_ids == ["p1-t2"]
            return CritiqueDecision(
                decision=CritiqueRoute.ACCEPT, plan_version=1, rationale="等标准导航修复已通过验收"
            )

        async def synthesize_report(self, *args, **kwargs):
            draft = await super().synthesize_report(*args, **kwargs)
            return draft.model_copy(
                update={
                    "content": draft.content.replace(
                        "## 结论", "研究发现及证据解释。" * 150 + "\n\n## 结论"
                    )
                }
            )

    gateway = RepairGateway()
    container = make_test_container(model=gateway)
    workflow = container.run_service.workflow
    # Register capability metadata but do not call a real provider in this test.
    registry = ToolRegistry()
    registry.register(
        MapItineraryTool(
            "test", transport=httpx.MockTransport(lambda request: pytest.fail("no real map call"))
        )
    )
    workflow.tool_runtime, _ = _runtime(
        registry, lambda request: pytest.fail("no real model call")
    )

    async def stage(state, agent, **kwargs):
        current = state.get("task")
        if agent != "researcher" or not current.required_capabilities:
            return ToolStageResult()
        if current.task_id == "p1-t2":
            failed = ToolCallRecord(
                tool_name="map_itinerary",
                agent="researcher",
                task_id=current.task_id,
                plan_version=1,
                capabilities=["map_itinerary"],
                arguments={},
                success=False,
                duration_ms=1,
                retryable=False,
                error="AMAP_POI_NO_MATCH: 地点条件不匹配",
            )
            raise RequiredToolError("地点条件不匹配", ToolStageResult(records=[failed]))
        verified = ToolCallRecord(
            call_id="map-proof",
            tool_name="map_itinerary",
            agent="researcher",
            task_id=current.task_id,
            plan_version=1,
            capabilities=["map_itinerary"],
            arguments={},
            success=True,
            duration_ms=1,
            navigation_urls=["https://uri.amap.com/navigation?verified=1"],
        )
        return ToolStageResult(records=[verified], context="导航已核实，source_id=map-proof")

    workflow._tool_stage = stage
    run = await container.run_service.execute_and_wait("对导航失败做局部修复后再交付")
    assert run.status is RunStatus.COMPLETED and run.report
    assert run.metrics.replan_count == 0 and run.metrics.supplement_rounds == 1
    assert (
        gateway.call_counts["create_plan"] == 1 and gateway.research_attempts.get("p1-t2", 0) == 0
    )
    assert gateway.research_attempts["p1-t4"] == 1 and not run.warnings
    assert len(run.plans) == 1 and run.plan.task_ids == ("p1-t1", "p1-t2", "p1-t3", "p1-t4")
    assert run.metrics.successful_tasks == 3 and run.metrics.failed_tasks == 1
    assert run.errors  # Original failure is retained as history, not hidden.
