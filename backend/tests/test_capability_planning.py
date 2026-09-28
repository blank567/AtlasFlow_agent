"""Capability routing is semantic planning, not query keyword classification."""

import json

import pytest
from atlasflow.agents.contracts import (
    CritiqueRoute,
    ResearchPlan,
    ResearchResult,
    ResearchTask,
    ReviewContext,
    RunPolicy,
    validate_task_capabilities,
)
from atlasflow.agents.gateway import OpenRouterModelGateway
from atlasflow.agents.tool_runtime import RequiredToolError
from atlasflow.schemas import ApprovalRequest, RunStatus, ToolCallRecord
from atlasflow.tools.base import BaseTool, Capability, ToolResult
from atlasflow.tools.calculator import CalculatorTool
from atlasflow.tools.map_route import AmapRouteTool
from atlasflow.tools.registry import ToolRegistry

from backend.tests.fakes import GatewayScenario, make_test_container
from backend.tests.test_tool_runtime import _call, _response, _runtime


class WeatherTool(BaseTool):
    """A new tool works without changing workflow, gateway or runtime branches."""

    name = "weather_provider_a"
    description = "Retrieve weather observations"
    capabilities = (Capability("weather_lookup", "查询天气观测", True),)

    async def run(self, arguments, context):
        return ToolResult(success=True, data={"summary": "Observed weather with timestamp"})


class AlternativeWeatherTool(WeatherTool):
    name = "weather_provider_b"


def raw_task(task_id="T1", capabilities=(), fresh=False):
    return {
        "task_id": task_id,
        "title": "研究",
        "objective": "回答目标",
        "success_criteria": ["回答问题"],
        "priority": 1,
        "dependencies": [],
        "requires_fresh_data": fresh,
        "required_capabilities": list(capabilities),
    }


@pytest.mark.asyncio
async def test_new_capability_supports_multiple_implementations_and_model_choice():
    registry = ToolRegistry()
    registry.register(WeatherTool())
    registry.register(AlternativeWeatherTool())
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert payload["tool_choice"] == "required"
            assert {item["function"]["name"] for item in payload["tools"]} == {
                "weather_provider_a",
                "weather_provider_b",
            }
            return _response(_call("weather_provider_b", {}))
        return _response({"content": "done"})

    runtime, _ = _runtime(registry, handler)
    stage = await runtime.gather(
        run_id="new-tool",
        agent="researcher",
        prompt="天气",
        policy=RunPolicy(),
        required_capabilities=["weather_lookup"],
        requires_fresh_data=True,
        task_id="T1",
        plan_version=1,
    )
    assert stage.records[0].capabilities == ["weather_lookup"]
    assert stage.records[0].plan_version == 1
    assert "Observed weather" in stage.context


@pytest.mark.parametrize("invalid", ["missing", "unknown", "fresh_calculator"])
@pytest.mark.asyncio
async def test_planner_repairs_missing_unknown_and_inconsistent_requirements(invalid):
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    registry.register(WeatherTool())
    correct = [raw_task(capabilities=["weather_lookup"], fresh=True), raw_task("T2")]

    class Client:
        def __init__(self):
            self.requests = []

        async def chat(self, **kwargs):
            self.requests.append(kwargs)
            tasks = json.loads(json.dumps(correct))
            if len(self.requests) == 1:
                if invalid == "missing":
                    tasks[0].pop("requires_fresh_data")
                elif invalid == "unknown":
                    tasks[0]["required_capabilities"] = ["invented_tool"]
                else:
                    tasks[0]["required_capabilities"] = ["calculator"]
            return {"content": json.dumps({"rationale": "test", "tasks": tasks})}

    client = Client()
    plan = await OpenRouterModelGateway(client, "test").create_plan(
        "今日天气", capability_catalog=registry.capability_catalog()
    )
    assert len(client.requests) == 2
    assert plan.tasks[0].required_capabilities == ["weather_lookup"]
    schema = client.requests[0]["response_format"]["json_schema"]["schema"]
    assert (
        "weather_lookup"
        in schema["properties"]["tasks"]["items"]["properties"]["required_capabilities"]["items"][
            "enum"
        ]
    )


def test_legacy_defaults_and_registry_constraints():
    task = raw_task()
    task.pop("requires_fresh_data")
    task.pop("required_capabilities")
    legacy = ResearchTask(**task, plan_version=1)
    assert not legacy.requires_fresh_data and legacy.required_capabilities == []
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    with pytest.raises(ValueError, match="unknown capabilities"):
        validate_task_capabilities(
            [ResearchTask(**raw_task(capabilities=["unknown"]), plan_version=1)],
            registry.capability_catalog(),
        )
    with pytest.raises(ValueError, match="unique"):
        ResearchTask(**raw_task(capabilities=["calculator", "calculator"]), plan_version=1)


@pytest.mark.asyncio
async def test_missing_map_configuration_is_visible_and_prevents_model_execution():
    registry = ToolRegistry()
    registry.register(AmapRouteTool(""))
    catalog = registry.capability_catalog()
    assert not catalog[0]["available"]
    # Missing configuration must not erase user intent at the planning boundary.
    validate_task_capabilities(
        [ResearchTask(**raw_task(capabilities=["map_route"]), plan_version=1)], catalog
    )
    runtime, _ = _runtime(registry, lambda _: pytest.fail("must not call model"))
    with pytest.raises(RequiredToolError, match="AMAP_API_KEY"):
        await runtime.gather(
            run_id="missing",
            agent="researcher",
            prompt="导航",
            policy=RunPolicy(),
            required_capabilities=["map_route"],
        )


@pytest.mark.parametrize(
    "task_id,version,fresh,reusable",
    [
        ("T1", 1, False, True),
        ("T2", 1, False, False),
        ("T1", 2, False, False),
        ("T1", 1, True, False),
        ("T1", None, False, False),
    ],
)
@pytest.mark.asyncio
async def test_capability_proof_is_task_and_plan_scoped(task_id, version, fresh, reusable):
    registry = ToolRegistry()
    registry.register(WeatherTool())
    prior = ToolCallRecord(
        tool_name="weather_provider_a",
        agent="researcher",
        task_id=task_id,
        plan_version=version,
        capabilities=["weather_lookup"],
        arguments={},
        success=True,
        duration_ms=1,
        summary="prior weather",
    )
    runtime, _ = _runtime(registry, lambda _: _response({"content": "done"}))
    kwargs = {
        "run_id": "scope",
        "agent": "researcher",
        "prompt": "天气",
        "policy": RunPolicy(),
        "required_capabilities": ["weather_lookup"],
        "requires_fresh_data": fresh,
        "task_id": "T1",
        "plan_version": 1,
        "prior_calls": [prior],
    }
    if reusable:
        assert not (await runtime.gather(**kwargs)).records
    else:
        with pytest.raises(RequiredToolError, match="no usable result"):
            await runtime.gather(**kwargs)


@pytest.mark.asyncio
async def test_concept_question_does_not_force_search_from_keywords():
    container = make_test_container(scenario=GatewayScenario(initial_task_count=2))
    runtime, _ = _runtime(container.registry, lambda _: _response({"content": "done"}))
    container.run_service.workflow.tool_runtime = runtime
    run = await container.run_service.execute_and_wait("解释汇率概念，不查询当前数值，也不需要导航")
    assert run.status is RunStatus.COMPLETED
    assert run.tool_calls == []


@pytest.mark.asyncio
async def test_critic_supplement_preserves_and_executes_capability_requirements():
    container = make_test_container(
        scenario=GatewayScenario(
            initial_task_count=2,
            critique_routes=[CritiqueRoute.SUPPLEMENT, CritiqueRoute.ACCEPT],
            task_capabilities={"p1-t3": ["calculator"]},
        )
    )

    def handler(request):
        choice = json.loads(request.content).get("tool_choice")
        return _response(
            _call("calculator", {"expression": "2+3"})
            if isinstance(choice, dict)
            else {"content": "done"}
        )

    runtime, _ = _runtime(container.registry, handler)
    container.run_service.workflow.tool_runtime = runtime
    run = await container.run_service.execute_and_wait("第一次两个任务，审查追加一个计算任务")
    assert run.status is RunStatus.COMPLETED
    assert len(run.plan.tasks) == 3
    assert run.plan.tasks[-1].required_capabilities == ["calculator"]
    assert [(call.task_id, call.summary) for call in run.tool_calls] == [("p1-t3", "5")]


@pytest.mark.asyncio
async def test_invalid_human_capability_edit_keeps_run_waiting_for_approval():
    container = make_test_container(scenario=GatewayScenario(initial_task_count=2))
    run = await container.run_service.execute_and_wait("解释概念", auto_approve=False)
    edited = run.plan.model_copy(deep=True)
    edited.tasks[0].required_capabilities = ["unknown"]
    with pytest.raises(ValueError, match="unknown capabilities"):
        await container.run_service.resolve_approval(
            run.id, ApprovalRequest(action="edit", edited_plan=edited)
        )
    assert (await container.store.get(run.id)).status is RunStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_identical_read_only_calls_reuse_only_within_one_stage():
    class CountingCalculator(CalculatorTool):
        executions = 0

        async def run(self, arguments, context):
            self.executions += 1
            return await super().run(arguments, context)

    tool = CountingCalculator()
    registry = ToolRegistry()
    registry.register(tool)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) % 3 == 0:
            return _response({"content": "done"})
        call = _call("calculator", {"expression": "2+3"})
        call["tool_calls"][0]["id"] = f"call-{len(requests)}"
        return _response(call)

    runtime, _ = _runtime(registry, handler)
    for index in (1, 2):
        stage = await runtime.gather(
            run_id=f"dedup-{index}", agent="researcher", prompt="算数", policy=RunPolicy()
        )
        assert len(stage.records) == 2
        assert stage.records[1].reused_from_call_id == stage.records[0].call_id
        assert stage.records[1].duration_ms == 0
        assert tool.executions == index


@pytest.mark.asyncio
async def test_critic_uses_same_dynamic_schema_and_repairs_omitted_capabilities():
    registry = ToolRegistry()
    registry.register(WeatherTool())
    plan = ResearchPlan(
        plan_version=1,
        rationale="test",
        tasks=[ResearchTask(**raw_task(task_id), plan_version=1) for task_id in ("T1", "T2")],
    )
    results = [
        ResearchResult(
            task_id=task.task_id, plan_version=1, summary="done", findings=["done"], confidence=0.8
        )
        for task in plan.tasks
    ]
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
        policy=RunPolicy(),
    )

    class Client:
        def __init__(self):
            self.requests = []

        async def chat(self, **kwargs):
            self.requests.append(kwargs)
            task = raw_task("T3", ["weather_lookup"], True)
            if len(self.requests) == 1:
                task.pop("required_capabilities")
            return {
                "content": json.dumps(
                    {
                        "decision": "supplement",
                        "rationale": "缺少天气",
                        "issues": [],
                        "replan_reason": None,
                        "supplemental_tasks": [task],
                    }
                )
            }

    client = Client()
    decision = await OpenRouterModelGateway(client, "test").review_research(
        "天气", plan, results, context=context, capability_catalog=registry.capability_catalog()
    )
    assert len(client.requests) == 2
    assert decision.supplemental_tasks[0].required_capabilities == ["weather_lookup"]
    schema = client.requests[0]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["supplemental_tasks"]["items"]["properties"][
        "required_capabilities"
    ]["items"]["enum"] == ["weather_lookup"]


def test_capability_definitions_and_agent_roles_are_enforced():
    class ConflictingWeather(WeatherTool):
        name = "conflicting_weather"
        capabilities = (Capability("weather_lookup", "different semantics", False),)

    class ResearchOnly(WeatherTool):
        allowed_agents = ("researcher",)

    registry = ToolRegistry()
    registry.register(ResearchOnly())
    assert registry.available_for("researcher")
    assert registry.available_for("planner") == []
    with pytest.raises(ValueError, match="conflicting capability"):
        registry.register(ConflictingWeather())
