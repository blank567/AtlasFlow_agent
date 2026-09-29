"""Regression of the Beijing trip: mixed tools, OR capabilities, budget and intent."""

import json

import httpx
import pytest
from atlasflow.agents.contracts import (
    CritiqueRoute,
    QualityRoute,
    ResearchTask,
    RunPolicy,
    validate_task_capabilities,
)
from atlasflow.agents.gateway import _task_schema
from atlasflow.agents.tool_runtime import ToolBudgetExhausted, ToolRuntime
from atlasflow.schemas import RunEventType, RunStatus
from atlasflow.tools.base import BaseTool, Capability, ToolContext, ToolResult
from atlasflow.tools.calculator import CalculatorTool
from atlasflow.tools.map_itinerary import MapItineraryTool
from atlasflow.tools.map_route import AmapRouteTool
from atlasflow.tools.registry import ToolRegistry
from pydantic import ValidationError

from backend.tests.fakes import GatewayScenario, make_test_container
from backend.tests.test_tool_runtime import _amap_handler, _call, _client, _response, _runtime


class Search(BaseTool):
    name = "web_search"
    capabilities = (Capability("web_search", "search", True),)
    description = "search"

    async def run(self, arguments, context):
        return ToolResult(success=True, data={"summary": "official public information"})


class PrivatePOI(Search):
    name = "poi_details"
    capabilities = (Capability("poi_details", "poi", True),)
    transient_output = True

    async def run(self, arguments, context):
        return ToolResult(success=True, data={"provider": "amap", "secret_candidate": "PRIVATE_PROVIDER_CONTENT", "distance_m": 123456789})

    @staticmethod
    def safe_summary(result):
        return "地点已核对，营业安排须查官网"


def registry():
    result = ToolRegistry()
    result.register(Search())
    result.register(PrivatePOI())
    result.register(AmapRouteTool("amap-test-key", transport=httpx.MockTransport(_amap_handler)))
    result.register(MapItineraryTool("amap-test-key", transport=httpx.MockTransport(_amap_handler)))
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("alternative", [False, True])
async def test_search_then_poi_then_route_continues_without_private_payload(alternative):
    replies = iter([
        _call("web_search", {}), _call("poi_details", {}),
        _call("map_itinerary", {"city": "北京", "stops": ["起点", "景点", "酒店"], "mode": "walking"}),
    ])
    seen = []

    def decide(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert "PRIVATE_PROVIDER_CONTENT" not in request.content.decode()
        assert "123456789" not in request.content.decode()
        if len(seen) == 3:
            safe = json.loads(payload["messages"][-1]["content"])
            assert safe["data"] == {} and safe["success"]
        return _response(next(replies))

    runtime, events = _runtime(registry(), decide)
    stage = await runtime.gather(
        run_id="composite", agent="researcher", prompt="补充雨天路线", policy=RunPolicy(),
        required_capabilities=["web_search", "poi_details"] + ([] if alternative else ["map_itinerary"]),
        capability_alternatives=[["map_route", "map_itinerary"]] if alternative else [],
        requires_fresh_data=True,
    )
    assert [item.tool_name for item in stage.records] == ["web_search", "poi_details", "map_itinerary"]
    assert all(item.success for item in stage.records)
    assert len(stage.records[-1].navigation_urls) == 2
    persisted = stage.context + json.dumps([event.model_dump(mode="json") for event in events])
    assert "PRIVATE_PROVIDER_CONTENT" not in persisted and "123456789" not in persisted


@pytest.mark.asyncio
async def test_invalid_mode_corrected_without_charging_tool_budget():
    args = {"city": "北京", "origin": "起点", "destination": "终点", "mode": "transit"}
    replies = iter([_call("map_route", args), _call("map_route", {**args, "mode": "walking"})])
    runtime, events = _runtime(registry(), lambda _: _response(next(replies)))
    stage = await runtime.gather(run_id="mode", agent="researcher", prompt="路线", policy=RunPolicy(max_tool_calls_per_turn=1, max_tool_calls_per_run=1), required_capabilities=["map_route"])
    assert len(stage.records) == 1 and stage.records[0].success
    rejection = events[0]
    assert rejection.data["executed"] is False and rejection.data["budget_counted"] is False
    assert "TOOL_ARGUMENTS_INVALID" in rejection.data["error"]
    assert "walking" in rejection.data["error"] and "driving" in rejection.data["error"]


def test_alternative_contract_schema_and_unknown_capability_checks():
    raw = {"task_id": "S1", "title": "路线", "objective": "路线二选一", "success_criteria": ["导航"], "priority": 1, "plan_version": 1, "requires_fresh_data": True, "capability_alternatives": [["map_route", "map_itinerary"]]}
    task = ResearchTask(**raw)
    validate_task_capabilities([task], registry().capability_catalog())
    assert "capability_alternatives" in _task_schema(registry().capability_catalog())["properties"]
    with pytest.raises(ValidationError):
        ResearchTask(**{**raw, "required_capabilities": ["map_route"]})
    with pytest.raises(ValueError, match="unknown capabilities"):
        validate_task_capabilities([ResearchTask(**{**raw, "capability_alternatives": [["map_route", "invented"]]})], registry().capability_catalog())
    legacy = ResearchTask(**{**raw, "capability_alternatives": [], "required_capabilities": ["map_route"]})
    assert legacy.capability_alternatives == []


@pytest.mark.parametrize("replacement", ["北京大学", "北京大学东门(地铁站)", "北京大学西门"])
def test_explicit_campus_gate_cannot_be_downgraded(replacement):
    tool = AmapRouteTool("test")
    context = ToolContext(run_id="gate", agent_name="researcher", user_query="未来三天北京游玩，从北大东门出发，夜间住酒店")
    assert "MAP_LOCATION_CONSTRAINT" in tool.validate_context({"city": "北京", "origin": replacement, "destination": "圆明园"}, context)
    assert tool.validate_context({"city": "北京", "origin": {"name": "北京大学", "entrance": "东门"}, "destination": "北京大学中关新园"}, context) is None


@pytest.mark.asyncio
async def test_gate_guard_before_http_and_budget_then_corrected_request():
    replies = iter([_call("map_route", {"city": "北京", "origin": origin, "destination": "终点", "mode": "walking"}) for origin in ["北京大学", "北京大学东门"]])
    runtime, events = _runtime(registry(), lambda _: _response(next(replies)))
    stage = await runtime.gather(run_id="gate", agent="researcher", prompt="行程", user_query="从北大东门出发", policy=RunPolicy(), required_capabilities=["map_route"])
    assert len(stage.records) == 1 and stage.records[0].success
    assert "MAP_LOCATION_CONSTRAINT" in events[0].data["error"]


@pytest.mark.asyncio
async def test_exhausted_budget_never_calls_model_again():
    runtime, _ = _runtime(registry(), lambda _: pytest.fail("no model request after budget exhaustion"))
    await runtime.budget.reserve("empty", already_used=0, limit=1)
    with pytest.raises(ToolBudgetExhausted) as error:
        await runtime.gather(run_id="empty", agent="researcher", prompt="查询", policy=RunPolicy(max_tool_calls_per_turn=1, max_tool_calls_per_run=1), required_capabilities=["web_search"])
    assert error.value.stage.model_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("review", ["quorum", "critic", "quality"])
async def test_full_graph_does_not_replan_after_shared_budget_exhaustion(review):
    scenario = GatewayScenario(initial_task_count=2, linear_dependencies=True, task_capabilities={"p1-t1": ["calculator"]})
    if review == "quorum":
        scenario.task_capabilities["p1-t2"] = ["calculator"]
    elif review == "critic":
        scenario.critique_routes = [CritiqueRoute.REPLAN]
    else:
        scenario.quality_routes = [QualityRoute.REPLAN]
    container = make_test_container(scenario=scenario)
    tools = ToolRegistry()
    tools.register(CalculatorTool())
    runtime = ToolRuntime(registry=tools, client=_client(lambda _: _response(_call("calculator", {"expression": "1+1"}))), model="test/model", event_sink=container.store.append_event)
    container.run_service.workflow.tool_runtime = runtime
    run = await container.run_service.execute_and_wait("预算耗尽测试", policy=RunPolicy(max_tool_calls_per_turn=1, max_tool_calls_per_run=1))
    assert run.metrics.replan_count == 0
    assert len(run.tool_calls) == 1
    assert any("工具预算已耗尽" in warning for warning in run.warnings)
    if review == "quorum":
        assert run.status is RunStatus.FAILED
        failures = [event for event in run.events if event.event_type is RunEventType.NODE_FAILED and event.task_id == "p1-t2"]
        assert len(failures) == 1 and failures[0].status == "failed"
    else:
        assert run.status is RunStatus.COMPLETED_WITH_WARNINGS
        assert run.report
