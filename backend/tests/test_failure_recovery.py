"""Regressions from the Beijing itinerary failure trace (2026-09-27)."""

import json

import httpx
import pytest
from atlasflow.agents.contracts import ResearchTask, RunPolicy
from atlasflow.agents.gateway import OpenRouterModelGateway
from atlasflow.config import Settings
from atlasflow.observability import _sanitize_trace_outputs, configure_langsmith, trace_segment
from atlasflow.providers.openrouter import ProviderRequestError
from atlasflow.schemas import RunStatus
from atlasflow.tools.base import ToolContext
from atlasflow.tools.map_itinerary import MapItineraryArguments, MapItineraryTool
from atlasflow.tools.map_route import AmapRequestLimiter, AmapRouteArguments, AmapRouteTool
from atlasflow.tools.registry import ToolPermissionError, ToolRegistry

from backend.tests.fakes import (
    FakeModelGateway,
    FakeWebSearchTool,
    GatewayScenario,
    make_test_container,
)
from backend.tests.test_capability_planning import raw_task
from backend.tests.test_tool_runtime import _amap_handler, _call, _client, _response, _runtime


@pytest.mark.asyncio
async def test_planner_default_never_contacts_model_for_tools():
    registry = ToolRegistry()
    registry.register(FakeWebSearchTool())
    runtime, _ = _runtime(registry, lambda _: pytest.fail("Planner tool stage is disabled"))
    stage = await runtime.gather(
        run_id="plan", agent="planner", prompt="规划旅行", policy=RunPolicy()
    )
    assert stage.model_calls == 0 and not stage.records


@pytest.mark.asyncio
async def test_planner_optional_reconnaissance_has_one_call_and_no_maps():
    class ReconSearch(FakeWebSearchTool):
        planning_safe = True

    registry = ToolRegistry()
    registry.register(ReconSearch())
    registry.register(AmapRouteTool("key"))
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["web_search"]
        calls = _call("web_search", {})
        calls["tool_calls"].append(
            {
                "id": "second",
                "type": "function",
                "function": {"name": "web_search", "arguments": "{}"},
            }
        )
        return _response(calls)

    runtime, _ = _runtime(registry, handler)
    stage = await runtime.gather(
        run_id="recon",
        agent="planner",
        prompt="背景不明",
        policy=RunPolicy(planner_allow_research=True),
    )
    assert len(stage.records) == len(requests) == 1
    with pytest.raises(ToolPermissionError):
        await registry.execute("map_route", {}, ToolContext(run_id="plan", agent_name="planner"))


@pytest.mark.asyncio
async def test_json_truncation_retries_with_larger_budget_and_retains_finish_reason():
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            message, finish = {"content": '{"summary": "unfinished'}, "length"
        else:
            message, finish = (
                {
                    "content": json.dumps(
                        {
                            "summary": "已完成",
                            "findings": ["结论"],
                            "limitations": [],
                            "confidence": 0.8,
                        }
                    )
                },
                "stop",
            )
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": finish}]}
        )

    client = _client(handler)
    result = await OpenRouterModelGateway(client, "test").analyze_task(
        "原始问题", ResearchTask(**raw_task(), plan_version=1), tool_context="单独的证据"
    )
    assert result.summary == "已完成"
    assert [item["max_tokens"] for item in requests] == [5000, 10000]
    assert [item.finish_reason for item in client.recent_calls] == ["length", "stop"]
    assert client.recent_calls[0].content_length > 0
    assert "单独的证据" in requests[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_truncated_json_is_never_accepted_even_if_parseable():
    content = json.dumps({"summary": "x", "findings": ["x"], "limitations": [], "confidence": 0.9})
    client = _client(
        lambda _: httpx.Response(
            200, json={"choices": [{"message": {"content": content}, "finish_reason": "length"}]}
        )
    )
    with pytest.raises(ProviderRequestError, match="truncated"):
        await OpenRouterModelGateway(client, "test").analyze_task(
            "test", ResearchTask(**raw_task(), plan_version=1)
        )
    assert len(client.recent_calls) == 2


@pytest.mark.asyncio
async def test_native_call_truncation_recovers_without_executing_partial_arguments():
    registry = ToolRegistry()
    registry.register(FakeWebSearchTool())
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"choices": [{"message": _call("web_search", {}), "finish_reason": "length"}]},
            )
        return _response(_call("web_search", {}) if len(requests) == 2 else {"content": "done"})

    runtime, _ = _runtime(registry, handler)
    stage = await runtime.gather(
        run_id="truncated",
        agent="researcher",
        prompt="查询",
        policy=RunPolicy(),
        required_capabilities=["web_search"],
    )
    assert [x["max_tokens"] for x in requests[:2]] == [3000, 6000]
    assert len(stage.records) == 1


@pytest.mark.asyncio
async def test_map_formal_name_alias_and_unambiguous_selection():
    def handler(request):
        if request.url.path == "/v5/place/text":
            name = request.url.params["keywords"]
            primary = "圆明园遗址公园" if name == "圆明园" else name
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "pois": [
                        {"name": primary, "location": "116.4,39.9"},
                        {"name": primary + "-停车场", "location": "116.3,39.8"},
                    ],
                },
            )
        return _amap_handler(request)

    result = await AmapRouteTool("amap-test-key", transport=httpx.MockTransport(handler)).run(
        AmapRouteArguments(city="北京", origin="颐和园", destination="圆明园", mode="walking"),
        ToolContext(run_id="alias", agent_name="researcher"),
    )
    assert result.success


@pytest.mark.parametrize(
    "code,expected", [("10009", "Web 服务"), ("10003", "配额"), ("10005", "白名单")]
)
@pytest.mark.asyncio
async def test_amap_error_codes_survive_without_raw_key_or_provider_message(code, expected):
    tool = AmapRouteTool(
        "secret-key",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"status": "0", "infocode": code, "info": "raw secret-key"}
            )
        ),
    )
    result = await tool.run(
        AmapRouteArguments(city="北京", origin="起点", destination="终点", mode="walking"),
        ToolContext(run_id="error", agent_name="researcher"),
    )
    assert not result.success and code in result.error and expected in result.error
    assert "secret-key" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_four_leg_itinerary_succeeds_with_one_tool_slot_and_no_numeric_retention():
    registry = ToolRegistry()
    registry.register(
        MapItineraryTool("amap-test-key", transport=httpx.MockTransport(_amap_handler))
    )
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert len(requests) == 1  # No post-map model call can leak transient results into traces.
        return _response(
            _call(
                "map_itinerary",
                {
                    "city": "北京",
                    "mode": "walking",
                    "stops": ["北大", "景点一", "景点二", "景点三", "北大"],
                },
            )
        )

    runtime, _ = _runtime(registry, handler)
    stage = await runtime.gather(
        run_id="itinerary",
        agent="researcher",
        prompt="三景点往返",
        policy=RunPolicy(max_tool_calls_per_turn=1),
        required_capabilities=["map_itinerary"],
    )
    assert stage.records[0].success and len(stage.records[0].navigation_urls) == 4
    report = runtime.append_references("行程报告", [], stage.records)
    for url in stage.records[0].navigation_urls:
        assert url in report
    persisted = stage.records[0].model_dump_json() + stage.context
    assert "distance_m" not in persisted and "duration_s" not in persisted
    assert "12345" not in persisted and "6789" not in persisted
    assert "12345" not in json.dumps(
        _sanitize_trace_outputs(
            {
                "data": {
                    "provider": "amap",
                    "distance_m": 12345,
                    "navigation_urls": stage.records[0].navigation_urls,
                }
            }
        )
    )


@pytest.mark.asyncio
async def test_partial_itinerary_is_failure_not_complete_capability():
    def handler(request):
        if request.url.params.get("keywords") == "坏地点":
            return httpx.Response(200, json={"status": "1", "pois": []})
        return _amap_handler(request)

    tool = MapItineraryTool("amap-test-key", transport=httpx.MockTransport(handler))
    result = await tool.run(
        MapItineraryArguments(city="北京", stops=["起点", "景点", "坏地点"], mode="walking"),
        ToolContext(run_id="partial", agent_name="researcher"),
    )
    assert not result.success and "第 2 段" in result.error
    assert len(result.data["navigation_urls"]) == 1
    assert "distance_m" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_workflow_preserves_query_and_passes_dependency_results_to_tools():
    original = "从北大出发规划三个景点并返回学校"

    class RecordingGateway(FakeModelGateway):
        async def create_plan(self, query, *args, **kwargs):
            assert query == original
            assert kwargs["tool_context"] == ""
            return await super().create_plan(query, *args, **kwargs)

        async def analyze_task(self, query, *args, **kwargs):
            assert query == original
            assert kwargs["tool_context"]
            return await super().analyze_task(query, *args, **kwargs)

    model = RecordingGateway(
        GatewayScenario(
            initial_task_count=2,
            linear_dependencies=True,
            task_capabilities={"p1-t1": ["web_search"], "p1-t2": ["map_itinerary"]},
        )
    )
    container = make_test_container(model=model)
    registry = ToolRegistry()
    registry.register(FakeWebSearchTool())
    registry.register(
        MapItineraryTool("amap-test-key", transport=httpx.MockTransport(_amap_handler))
    )
    saw_dependencies = []

    def handler(request):
        payload = json.loads(request.content)
        choice = payload.get("tool_choice")
        if isinstance(choice, dict) and choice["function"]["name"] == "map_itinerary":
            prompt = payload["messages"][1]["content"]
            assert '"summary"' in prompt and "p1-t1" in prompt
            saw_dependencies.append(True)
            return _response(
                _call(
                    "map_itinerary",
                    {
                        "city": "北京",
                        "mode": "walking",
                        "stops": ["学校", "景点一", "景点二", "景点三", "学校"],
                    },
                )
            )
        return _response(
            _call("web_search", {}) if isinstance(choice, dict) else {"content": "done"}
        )

    runtime, _ = _runtime(registry, handler)
    container.run_service.workflow.tool_runtime = runtime
    run = await container.run_service.execute_and_wait(original)
    assert run.status is RunStatus.COMPLETED
    assert run.query == original and saw_dependencies
    assert not any(call.agent == "planner" for call in run.tool_calls)
    assert len(run.tool_calls[-1].navigation_urls) == 4


@pytest.mark.asyncio
async def test_amap_qps_retries_are_bounded_and_use_shared_limiter(monkeypatch):
    attempts = []
    delays = []

    async def no_wait(delay):
        delays.append(delay)

    monkeypatch.setattr("atlasflow.tools.map_route.asyncio.sleep", no_wait)

    def handler(request):
        attempts.append(request.url.path)
        if len(attempts) <= 2:
            return httpx.Response(200, json={"status": "0", "infocode": "10021"})
        return _amap_handler(request)

    tool = AmapRouteTool("amap-test-key", transport=httpx.MockTransport(handler),
                        request_limiter=AmapRequestLimiter(0))
    result = await tool.run(AmapRouteArguments(city="北京", origin="起点", destination="终点", mode="walking"),
                            ToolContext(run_id="qps", agent_name="researcher"))
    assert result.success and len(attempts) == 5
    assert 1 in delays and 2 in delays

    attempts.clear()

    def always_limited(request):
        attempts.append(request.url.path)
        return httpx.Response(200, json={"status": "0", "infocode": "10021"})

    limited = AmapRouteTool("key", transport=httpx.MockTransport(always_limited))
    result = await limited.run(AmapRouteArguments(city="北京", origin="起点", destination="终点", mode="walking"),
                               ToolContext(run_id="qps-fail", agent_name="researcher"))
    assert not result.success and len(attempts) == 3
    assert "10021" in result.error


@pytest.mark.asyncio
async def test_business_failure_marks_trace_segment_failed_without_uncaught_exception():
    configure_langsmith(Settings(_env_file=None, langsmith_tracing=False))
    async with trace_segment(atlasflow_run_id="business-failed", name="test", kind="initial") as handle:
        handle.set_outputs({"status": "failed", "final": True})
    assert handle.segment.status == "failed"
    assert "status=failed" in handle.segment.error
