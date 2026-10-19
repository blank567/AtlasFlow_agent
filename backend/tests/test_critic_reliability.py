"""Replay the Critic capability conflict and verify bounded, evidence-safe recovery.

All model/tool responses are offline fixtures; no production Run is replayed.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from atlasflow.agents.contracts import (
    CritiqueRoute,
    QualityRoute,
    ResearchPlan,
    ResearchTask,
    ReviewContext,
    RunPolicy,
)
from atlasflow.agents.gateway import OpenRouterModelGateway
from atlasflow.agents.output_validation import (
    StructuredOutputError,
    normalize_task_capabilities,
    validation_error_detail,
)
from atlasflow.agents.task_quality import can_degrade_critic_review
from atlasflow.providers.openrouter import ProviderRequestError
from atlasflow.schemas import RunEventType, RunStatus, ToolCallRecord
from atlasflow.tools.calculator import CalculatorTool
from atlasflow.tools.registry import ToolRegistry

from backend.tests.fakes import FakeWebSearchTool, make_test_container
from backend.tests.test_capability_planning import raw_task
from backend.tests.test_execution_recovery import harness
from backend.tests.test_presentation_acceptance import presentation_settings
from backend.tests.test_quality_output_recovery import Client
from backend.tests.test_task_acceptance import AcceptanceGateway, result
from backend.tests.test_tool_runtime import _call, _response, _runtime


CATALOG = [
    {"id": cap, "supports_fresh_data": True, "available": True}
    for cap in ("web_search", "web_fetch", "map_route", "map_itinerary", "new_capability")
]


def review_inputs(policy=None):
    tasks = [
        raw_task("T1", ["web_search"], True),
        raw_task("T2"),
        raw_task("T3", ["map_itinerary"], True),
    ]
    tasks[1]["dependencies"], tasks[2]["dependencies"] = ["T1"], ["T2"]
    plan = ResearchPlan(
        plan_version=1,
        rationale="资料、编排、导航",
        tasks=[ResearchTask(**item, plan_version=1) for item in tasks],
    )
    results = [result(item.task_id) for item in plan.tasks]
    context = ReviewContext(
        review_round=1,
        plan_version=1,
        plan_revision=0,
        initial_task_ids=list(plan.task_ids),
        completed_task_ids=list(plan.task_ids),
        supplement_rounds_used=0,
        supplement_rounds_remaining=1,
        current_task_count=3,
        expected_task_count=3,
        policy=policy or RunPolicy(),
    )
    return plan, results, context


def supplement(*, critical=False, broken=False):
    first = {**raw_task("T4", ["web_search", "web_fetch"], True), "repairs_task_ids": ["T1"]}
    second = {
        **raw_task("T5", ["map_itinerary"], True),
        "repairs_task_ids": ["T3"],
        "dependencies": ["T5"] if broken else ["T2"],
        "capability_alternatives": [["map_route", "map_itinerary"]],
    }
    return {
        "decision": "supplement",
        "rationale": "只修复已有缺口",
        "replan_reason": None,
        "issues": [
            {
                "severity": "critical" if critical else "warning",
                "code": "unresolved_critical_gap",
                "message": "部分范围尚待核验",
                "recommendation": "针对性补充",
                "task_id": None,
            }
        ],
        "supplemental_tasks": [first, second],
    }


def test_exact_failed_declaration_drops_only_logically_redundant_group():
    raw = supplement()["supplemental_tasks"][1]
    original = deepcopy(raw)
    normalized = normalize_task_capabilities(raw, CATALOG)
    assert normalized["required_capabilities"] == ["map_itinerary"]
    assert normalized["capability_alternatives"] == [] and raw == original
    assert normalized["repairs_task_ids"] == ["T3"] and normalized["dependencies"] == ["T2"]
    assert ResearchTask(**normalized, plan_version=1).requires_fresh_data


def test_nonoverlapping_alternative_group_is_preserved():
    raw = {
        **raw_task("T1", ["web_search"]),
        "capability_alternatives": [["web_search", "web_fetch"], ["map_route", "map_itinerary"]],
    }
    normalized = normalize_task_capabilities(raw, CATALOG)
    assert normalized["capability_alternatives"] == [["map_route", "map_itinerary"]]


def test_canonicalization_supports_new_registered_capabilities_without_tool_branches():
    raw = {
        **raw_task("T1", ["new_capability"]),
        "capability_alternatives": [["web_search", "new_capability"]],
    }
    assert normalize_task_capabilities(raw, CATALOG)["capability_alternatives"] == []


@pytest.mark.parametrize(
    "updates",
    [
        {"required_capabilities": ["unknown_capability"]},
        {"required_capabilities": ["map_itinerary", "map_itinerary"]},
        {"capability_alternatives": [["map_itinerary", "unknown_capability"]]},
        {"capability_alternatives": [["map_itinerary", "map_itinerary"]]},
        {"capability_alternatives": [["map_itinerary"]]},
        {"capability_alternatives": "map_itinerary"},
    ],
)
def test_unknown_or_malformed_declarations_are_not_silently_repaired(updates):
    raw = {**supplement()["supplemental_tasks"][1], **updates}
    assert normalize_task_capabilities(raw, CATALOG) == raw


@pytest.mark.asyncio
async def test_actual_critic_conflict_shape_succeeds_on_first_output_even_for_critical_repair():
    plan, results, context = review_inputs()
    raw = supplement(critical=True)
    original = deepcopy(raw)
    client = Client([raw])
    decision = await OpenRouterModelGateway(client, "test").review_research(
        "一日游研究", plan, results, context=context, capability_catalog=CATALOG
    )
    assert len(client.requests) == 1 and decision.decision is CritiqueRoute.SUPPLEMENT
    assert decision.supplemental_tasks[1].required_capabilities == ["map_itinerary"]
    assert decision.supplemental_tasks[1].capability_alternatives == [] and raw == original


@pytest.mark.asyncio
async def test_planner_uses_the_same_semantics_preserving_canonicalization():
    task = {
        **raw_task("T1", ["map_itinerary"], True),
        "capability_alternatives": [["map_route", "map_itinerary"]],
    }
    client = Client([{"rationale": "最小任务", "tasks": [task, raw_task("T2")]}])
    plan = await OpenRouterModelGateway(client, "test").create_plan(
        "行程研究", capability_catalog=CATALOG
    )
    assert plan.tasks[0].required_capabilities == ["map_itinerary"]
    assert plan.tasks[0].capability_alternatives == [] and len(client.requests) == 1
    prompt = client.requests[0]["messages"][0]["content"]
    assert "用户没有明确数量时" in prompt and "success_criteria不得自行写成" in prompt


@pytest.mark.asyncio
async def test_retry_receives_actionable_message_instead_of_just_value_error():
    plan, results, context = review_inputs()
    client = Client([supplement(broken=True), supplement()])
    decision = await OpenRouterModelGateway(client, "test").review_research(
        "研究", plan, results, context=context, capability_catalog=CATALOG
    )
    repair = client.requests[1]["messages"][-1]["content"]
    assert "supplemental_tasks.1" in repair and "a task cannot depend on itself" in repair
    assert decision.supplemental_tasks[1].dependencies == ["T2"]


def test_diagnostics_exclude_raw_input_and_exception_context():
    invalid = {**raw_task("T5"), "dependencies": ["T5"], "objective": "private-raw-input"}
    with pytest.raises(ValidationError) as caught:
        ResearchTask(**invalid, plan_version=1)
    detail = validation_error_detail(caught.value)
    assert "a task cannot depend on itself" in detail
    assert "private-raw-input" not in detail and "ctx" not in detail and "input_value" not in detail


@pytest.mark.asyncio
@pytest.mark.parametrize("critical,expected_safe", [(False, True), (True, False)])
async def test_only_valid_noncritical_issue_envelopes_authorize_a_recovery_hint(
    critical, expected_safe
):
    plan, results, context = review_inputs()
    client = Client([supplement(critical=critical, broken=True)])
    with pytest.raises(StructuredOutputError) as caught:
        await OpenRouterModelGateway(client, "test").review_research(
            "研究", plan, results, context=context, capability_catalog=CATALOG
        )
    assert caught.value.review_fallback_safe is expected_safe
    assert caught.value.outcomes == ("CONTRACT_INVALID", "CONTRACT_INVALID")
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_earlier_critical_issue_cannot_disappear_in_a_later_malformed_attempt():
    plan, results, context = review_inputs()
    client = Client([supplement(critical=True, broken=True), supplement(broken=True)])
    with pytest.raises(StructuredOutputError) as caught:
        await OpenRouterModelGateway(client, "test").review_research(
            "研究", plan, results, context=context, capability_catalog=CATALOG
        )
    assert not caught.value.review_fallback_safe


@pytest.mark.asyncio
async def test_unreadable_review_is_not_equivalent_to_no_critical_issues():
    class InvalidJson(Client):
        async def chat(self, **kwargs):
            self.requests.append(deepcopy(kwargs))
            return {"content": "not JSON", "_atlasflow_finish_reason": "stop"}

    client = InvalidJson([])
    plan, results, context = review_inputs()
    with pytest.raises(StructuredOutputError) as caught:
        await OpenRouterModelGateway(client, "test").review_research(
            "研究", plan, results, context=context, capability_catalog=CATALOG
        )
    assert not caught.value.review_fallback_safe and "INVALID_JSON" in caught.value.outcomes


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_issues", "invalid_issue", "replan"])
async def test_unknown_or_replan_issue_envelope_never_authorizes_recovery(case):
    plan, results, context = review_inputs()
    raw = supplement(broken=True)
    if case == "missing_issues":
        raw.pop("issues")
    elif case == "invalid_issue":
        raw["issues"][0].pop("recommendation")
    else:
        raw["decision"] = "replan"
    client = Client([raw])
    with pytest.raises(StructuredOutputError) as caught:
        await OpenRouterModelGateway(client, "test").review_research(
            "研究", plan, results, context=context, capability_catalog=CATALOG
        )
    assert not caught.value.review_fallback_safe


@pytest.mark.parametrize("case", ["legacy", "not_accepted", "old_plan", "wrong_context"])
def test_every_current_task_requires_explicit_acceptance_before_degradation(case):
    plan, results, context = review_inputs()
    results = [item.model_copy(update={"execution_accepted": True}) for item in results]
    assert can_degrade_critic_review(plan, results, context)
    if case == "wrong_context":
        context = context.model_copy(update={"plan_version": 2})
    else:
        changes = {
            "legacy": {"execution_accepted": None},
            "not_accepted": {"execution_accepted": False},
            "old_plan": {"plan_version": 2},
        }[case]
        results[0] = results[0].model_copy(update=changes)
    assert not can_degrade_critic_review(plan, results, context)


class BrokenReviewGateway(AcceptanceGateway):
    def __init__(self, *, critical=False, network_error=False):
        super().__init__(repair=False)
        self.client = Client([supplement(critical=critical, broken=True)])
        self.network_error = network_error

    async def review_research(self, query, plan, results, **kwargs):
        self._called("review_research")
        if self.network_error:
            raise ProviderRequestError("fixture provider HTTP 400")
        return await OpenRouterModelGateway(self.client, "test").review_research(
            query, plan, results, **kwargs
        )


@pytest.mark.asyncio
async def test_safe_output_failure_reaches_synthesis_and_independent_quality_without_fake_accept():
    gateway = BrokenReviewGateway()
    container = make_test_container(model=gateway, settings=presentation_settings())
    run = await container.run_service.execute_and_wait("研究流程展示")
    assert run.status is RunStatus.COMPLETED and run.report and not run.warnings
    assert not run.critique_history and run.quality_history[-1].decision is QualityRoute.ACCEPT
    assert any(error.code == "critic_output_degraded" for error in run.errors)
    review = next(event for event in run.events if event.event_type is RunEventType.REVIEW_DECIDED)
    assert review.data["decision"] == "review_degraded" and review.status == "degraded"
    assert gateway.call_counts["synthesize_report"] == gateway.call_counts["evaluate_report"] == 1
    finalizer = next(
        event
        for event in run.events
        if event.node == "finalizer" and event.event_type is RunEventType.NODE_SUCCEEDED
    )
    assert finalizer.data["historical_operational_warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["critical", "network", "strict", "required_supplement"])
async def test_unreviewed_recovery_never_fakes_a_critic_or_quality_accept(case):
    gateway = BrokenReviewGateway(critical=case == "critical", network_error=case == "network")
    settings = presentation_settings(research_accept_partial_results=case != "strict")
    policy = RunPolicy(required_supplement_rounds=1 if case == "required_supplement" else 0)
    container = make_test_container(model=gateway, settings=settings)
    run = await container.run_service.execute_and_wait("保持关键约束", policy=policy)
    if case == "strict":
        assert run.status is RunStatus.FAILED and run.error
    else:
        assert run.status is RunStatus.COMPLETED_WITH_WARNINGS and run.report and run.warnings
        assert "未获得完整质量验收" in run.report
        assert not run.critique_history and not run.quality_history
        assert any(error.code == "critic_delivery_fallback" for error in run.errors)
    assert gateway.call_counts.get("synthesize_report", 0) == 0
    assert not any(error.code == "critic_output_degraded" for error in run.errors)


@pytest.mark.asyncio
async def test_quality_rejection_is_still_effective_after_review_degradation():
    gateway = BrokenReviewGateway()
    gateway.scenario.quality_routes = [QualityRoute.REVISE, QualityRoute.REVISE]
    container = make_test_container(model=gateway, settings=presentation_settings())
    run = await container.run_service.execute_and_wait("不能绕过独立质量门禁")
    assert run.status is RunStatus.COMPLETED_WITH_WARNINGS and run.warnings
    assert run.metrics.revision_count == 1 and gateway.call_counts["evaluate_report"] == 2


@pytest.mark.asyncio
async def test_review_degradation_is_durable_and_readable_after_reconcile(tmp_path):
    gateway = BrokenReviewGateway()
    runtime, store, _, service, _ = await harness(tmp_path, gateway=gateway)
    try:
        run = await service.execute_and_wait(
            "持久化审查降级", policy=RunPolicy(knowledge_mode="off")
        )
        assert run.status is RunStatus.COMPLETED and not run.warnings
        before = gateway.call_counts.copy()
        await service.reconcile_startup()
        restored = await store.get(run.id)
        assert restored.status is RunStatus.COMPLETED and gateway.call_counts == before
        assert any(error.code == "critic_output_degraded" for error in restored.errors)
    finally:
        await store.close()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["researcher", "critic", "quality_gate"])
async def test_reviewing_agents_can_use_ten_calls_but_never_an_eleventh(agent):
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        call = _call("calculator", {"expression": f"{len(requests)}+1"})
        call["tool_calls"][0]["id"] = f"call-{len(requests)}"
        return _response(call)

    runtime, events = _runtime(registry, respond)
    stage = await runtime.gather(
        run_id=f"budget-{agent}", agent=agent, prompt="计算", policy=RunPolicy()
    )
    assert len(stage.records) == 10 and all(call.success for call in stage.records)
    assert "本次最多 10 次工具调用" in requests[0]["messages"][0]["content"]
    assert any(event.event_type is RunEventType.TOOL_BUDGET_EXHAUSTED for event in events)


@pytest.mark.asyncio
async def test_total_forty_call_budget_is_not_bypassed_by_larger_review_stage():
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    runtime, _ = _runtime(
        registry, lambda _: _response(_call("calculator", {"expression": "1000+1"}))
    )
    prior = [
        ToolCallRecord(
            call_id=f"old-{index}",
            tool_name="calculator",
            arguments={"expression": str(index)},
            success=True,
            duration_ms=1,
        )
        for index in range(39)
    ]
    stage = await runtime.gather(
        run_id="total-budget", agent="critic", prompt="复核", policy=RunPolicy(), prior_calls=prior
    )
    assert len(stage.records) == 1 and stage.records[0].success


@pytest.mark.asyncio
async def test_planner_still_has_only_one_lightweight_research_call():
    class ReconSearch(FakeWebSearchTool):
        planning_safe = True

    registry = ToolRegistry()
    registry.register(ReconSearch())
    runtime, _ = _runtime(registry, lambda _: _response(_call("web_search", {})))
    stage = await runtime.gather(
        run_id="light-planner",
        agent="planner",
        prompt="背景",
        policy=RunPolicy(planner_allow_research=True),
    )
    assert len(stage.records) == 1
