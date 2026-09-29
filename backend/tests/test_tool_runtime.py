from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.tool_runtime import RequiredToolError, ToolRuntime
from atlasflow.observability import _sanitize_trace_outputs
from atlasflow.providers.openrouter import OpenRouterClient
from atlasflow.schemas import RunEvent, RunEventType, RunStatus, ToolCallRecord
from atlasflow.tools import ToolContext, ToolRegistry
from atlasflow.tools.calculator import CalculatorTool
from atlasflow.tools.map_route import AmapRouteArguments, AmapRouteTool
from atlasflow.tools.web_search import OpenRouterWebSearchTool, WebSearchArguments

from backend.tests.fakes import GatewayScenario, make_test_container


def _response(message: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", **message}}]})


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": f"call-{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def _client(handler) -> OpenRouterClient:
    return OpenRouterClient(
        api_key="test-key",
        base_url="https://openrouter.test/api/v1",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )


def _runtime(registry, handler):
    events: list[RunEvent] = []

    async def emit(_run_id: str, event: RunEvent) -> None:
        events.append(event)

    return ToolRuntime(
        registry=registry, client=_client(handler), model="test/model", event_sink=emit
    ), events


@pytest.mark.asyncio
async def test_native_call_round_trip_uses_validated_result_and_preserves_call_id() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    payloads = []

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        # Reproduce endpoints that support tools/tool_choice but reject the
        # optional parallel switch when require_parameters is enabled.
        if "parallel_tool_calls" in payload:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "message": "No endpoints found that can handle the requested parameters",
                    }
                },
            )
        if len(payloads) == 1:
            assert payload["tool_choice"] == {
                "type": "function",
                "function": {"name": "calculator"},
            }
            assert "parallel_tool_calls" not in payload
            assert payload["provider"] == {"require_parameters": True}
            return _response(_call("calculator", {"expression": "1+1"}))
        tool_message = payload["messages"][-1]
        assert tool_message["role"] == "tool"
        assert tool_message["tool_call_id"] == "call-calculator"
        assert json.loads(tool_message["content"])["data"]["result"] == 2
        return _response({"content": "done"})

    runtime, events = _runtime(registry, handler)
    result = await runtime.gather(
        run_id="native",
        agent="researcher",
        prompt="1+1=?",
        policy=RunPolicy(max_tool_calls_per_turn=1, max_tool_calls_per_run=2),
        required_capabilities=["calculator"],
    )
    assert result.model_calls == 2
    assert len(result.records) == 1
    assert result.records[0].success
    assert "1+1 = 2" in result.context
    assert [event.event_type for event in events] == [
        RunEventType.TOOL_REQUESTED,
        RunEventType.TOOL_SUCCEEDED,
    ]


def _batch() -> dict[str, Any]:
    calls = []
    for index, expression in enumerate(("1+1", "3*4")):
        call = _call("calculator", {"expression": expression})["tool_calls"][0]
        call["id"] = f"call-{index}"
        calls.append(call)
    return {"content": None, "tool_calls": calls}


@pytest.mark.asyncio
async def test_multiple_returned_calls_execute_sequentially_with_matching_results() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "parallel_tool_calls" not in payload
        if len(requests) == 1:
            return _response(_batch())
        results = [message for message in payload["messages"] if message["role"] == "tool"]
        assert [item["tool_call_id"] for item in results] == ["call-0", "call-1"]
        assert [json.loads(item["content"])["data"]["result"] for item in results] == [2, 12]
        return _response({"content": "done"})

    runtime, events = _runtime(registry, handler)
    stage = await runtime.gather(
        run_id="batch",
        agent="researcher",
        prompt="计算两个算式",
        policy=RunPolicy(),
    )
    assert [record.summary for record in stage.records] == ["2", "12"]
    assert [(event.event_type, event.data["call_id"]) for event in events] == [
        (RunEventType.TOOL_REQUESTED, "call-0"),
        (RunEventType.TOOL_SUCCEEDED, "call-0"),
        (RunEventType.TOOL_REQUESTED, "call-1"),
        (RunEventType.TOOL_SUCCEEDED, "call-1"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_scope", ["turn", "run"])
async def test_batch_respects_budget_and_never_sends_partial_tool_results(budget_scope) -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert len(requests) == 1, (
            "a partially executed batch must not be sent back to the provider"
        )
        return _response(_batch())

    prior = (
        []
        if budget_scope == "turn"
        else [
            ToolCallRecord(
                tool_name="calculator",
                arguments={"expression": "0+0"},
                success=True,
                duration_ms=1,
            )
        ]
    )
    runtime, events = _runtime(registry, handler)
    stage = await runtime.gather(
        run_id="bounded-batch",
        agent="researcher",
        prompt="计算两个算式",
        policy=RunPolicy(
            max_tool_calls_per_turn=1 if budget_scope == "turn" else 2, max_tool_calls_per_run=2
        ),
        prior_calls=prior,
        required_capabilities=["calculator"],
    )
    assert len(stage.records) == 1
    assert stage.records[0].summary == "2"
    assert events[-1].event_type is RunEventType.TOOL_BUDGET_EXHAUSTED
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_duplicate_batch_ids_fail_before_any_tool_execution() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    batch = _batch()
    batch["tool_calls"][1]["id"] = batch["tool_calls"][0]["id"]
    runtime, events = _runtime(registry, lambda _: _response(batch))
    with pytest.raises(RequiredToolError, match="duplicate id"):
        await runtime.gather(
            run_id="duplicate", agent="researcher", prompt="计算", policy=RunPolicy()
        )
    assert events == []


@pytest.mark.asyncio
async def test_parallel_branches_share_one_atomic_run_budget() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())

    def handler(request):
        payload = json.loads(request.content)
        return _response(
            {"content": "done"}
            if payload["messages"][-1]["role"] == "tool"
            else _call("calculator", {"expression": "7*8"})
        )

    runtime, events = _runtime(registry, handler)
    results = await asyncio.gather(
        *(
            runtime.gather(
                run_id="shared",
                agent="researcher",
                prompt="calculate",
                task_id=task_id,
                required_capabilities=["calculator"],
                policy=RunPolicy(max_tool_calls_per_turn=1, max_tool_calls_per_run=1),
            )
            for task_id in ("T1", "T2")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RequiredToolError) for result in results) == 1
    assert sum(event.event_type is RunEventType.TOOL_REQUESTED for event in events) == 1
    assert sum(event.event_type is RunEventType.TOOL_BUDGET_EXHAUSTED for event in events) == 1


@pytest.mark.asyncio
async def test_turn_budget_blocks_an_additional_request() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    runtime, events = _runtime(
        registry, lambda _: _response(_call("calculator", {"expression": "2+2"}))
    )
    result = await runtime.gather(
        run_id="turn",
        agent="researcher",
        prompt="calculate",
        policy=RunPolicy(max_tool_calls_per_turn=1),
        required_capabilities=["calculator"],
    )
    assert len(result.records) == 1
    assert events[-1].event_type is RunEventType.TOOL_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_disallowed_tool_and_bad_arguments_are_audited_failures() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    calls = iter(
        [
            _call("unregistered_shell", {"command": "anything"}),
            _call("calculator", {"expression": "1+1", "unexpected": True}),
            _call("calculator", {"expression": "1+1", "unexpected": True}),
        ]
    )
    runtime, events = _runtime(registry, lambda _: _response(next(calls)))
    with pytest.raises(RequiredToolError) as error:
        await runtime.gather(
            run_id="invalid",
            agent="researcher",
            prompt="calculate",
            policy=RunPolicy(),
            required_capabilities=["calculator"],
        )
    assert error.value.stage is not None
    assert error.value.stage.records == []
    assert sum(event.event_type is RunEventType.TOOL_FAILED for event in events) == 3
    assert all(event.data.get("budget_counted") is False for event in events)
    assert runtime.budget.remaining("invalid", already_used=0, limit=20) == 20


@pytest.mark.asyncio
async def test_provider_failure_keeps_already_executed_tool_records() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    responses = iter([_response(_call("calculator", {"expression": "1+1"})), httpx.Response(503)])
    runtime, _ = _runtime(registry, lambda _: next(responses))
    with pytest.raises(RequiredToolError) as error:
        await runtime.gather(
            run_id="provider-failure", agent="researcher", prompt="calculate", policy=RunPolicy()
        )
    assert error.value.stage.records[0].summary == "2"


def _amap_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.params["key"] == "amap-test-key"
    if request.url.path == "/v3/assistant/inputtips":
        assert request.url.params["city"] == "北京"
        keyword = request.url.params["keywords"]
        official = "北京大学(东门)" if keyword == "北京大学东门" else keyword
        return httpx.Response(
            200,
            json={
                "status": "1",
                "tips": [
                    {
                        "name": official,
                        "location": "116.4,39.9",
                        "id": "test-poi",
                        "district": "北京市海淀区",
                        "typecode": "991400",
                    }
                ],
            },
        )
    if request.url.path == "/v5/place/text":
        assert request.url.params["region"] == "北京"
        assert request.url.params["city_limit"] == "true"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "pois": [
                    {
                        "name": request.url.params["keywords"],
                        "location": "116.4,39.9",
                        "id": "test-poi",
                    }
                ],
            },
        )
    assert request.url.path == "/v5/direction/walking"
    return httpx.Response(
        200,
        json={
            "status": "1",
            "route": {
                "paths": [
                    {
                        "distance": "12345",
                        "cost": {"duration": "6789"},
                    }
                ]
            },
        },
    )


@pytest.mark.asyncio
async def test_map_is_transient_in_stage_events_records_context_and_trace(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACE_CONTENT", "true")
    # Content sanitizers are tested directly; no real tracing or provider calls.
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    arguments = {"city": "北京", "origin": "起点", "destination": "终点", "mode": "walking"}
    tool = AmapRouteTool("amap-test-key", transport=httpx.MockTransport(_amap_handler))
    result = await tool.run(
        AmapRouteArguments(**arguments), ToolContext(run_id="map", agent_name="researcher")
    )
    assert result.success
    assert result.data["distance_m"] == 12345
    assert result.data["duration_s"] == 6789
    for output in (result, result.model_dump()):
        trace = _sanitize_trace_outputs(output)
        assert trace["route_content_redacted"]
        assert "12345" not in json.dumps(trace)
        assert "6789" not in json.dumps(trace)

    registry = ToolRegistry()
    registry.register(tool)
    registry.register(CalculatorTool())

    def handler(request):
        payload = json.loads(request.content)
        if payload["messages"][-1]["role"] == "tool":
            transient = json.loads(payload["messages"][-1]["content"])
            assert transient["data"]["distance_m"] == 12345
            assert payload["tool_choice"] == "none"
            # A non-compliant provider must not copy the transient response to
            # another tool's persisted arguments.
            return _response(_call("calculator", {"expression": "12345+6789"}))
        return _response(_call("map_route", arguments))

    runtime, events = _runtime(registry, handler)
    stage = await runtime.gather(
        run_id="map",
        agent="researcher",
        prompt="导航",
        policy=RunPolicy(),
        required_capabilities=["map_route"],
    )
    persisted = json.dumps(
        {
            "records": [record.model_dump(mode="json") for record in stage.records],
            "events": [event.model_dump(mode="json") for event in events],
            "context": stage.context,
        },
        ensure_ascii=False,
    )
    assert "distance_m" not in persisted and "duration_s" not in persisted
    assert "12345" not in stage.context and "6789" not in stage.context
    assert stage.records[0].arguments == {"mode": "walking"}
    assert "amap-test-key" not in persisted
    assert stage.evidence == []
    assert len(stage.records) == 1
    assert stage.records[0].navigation_url.startswith("https://uri.amap.com/navigation?")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"status": "1", "pois": [{"name": "重名", "location": "116.4,39.9"}] * 2},
        {"status": "0", "info": "bad key"},
        {"status": "1", "pois": None},
    ],
)
async def test_map_ambiguity_or_bad_provider_response_fails_without_guessing(response) -> None:
    tool = AmapRouteTool(
        "amap-test-key", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    )
    result = await tool.run(
        AmapRouteArguments(city="北京", origin="重名", destination="终点", mode="walking"),
        ToolContext(run_id="map-fail", agent_name="researcher"),
    )
    assert not result.success
    assert "amap-test-key" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_web_search_retains_cited_summary_and_rejects_missing_sources() -> None:
    message = {
        "content": "某指标为 42，数据截至今天。",
        "annotations": [
            {
                "url_citation": {
                    "url": "https://example.test/data",
                    "title": "数据来源",
                    "start_index": 0,
                    "end_index": 7,
                }
            }
        ],
    }
    tool = OpenRouterWebSearchTool(_client(lambda _: _response(message)), "test/search")
    result = await tool.run(
        WebSearchArguments(query="最新指标"), ToolContext(run_id="web", agent_name="researcher")
    )
    assert result.success
    assert result.evidence[0].content == message["content"][:7]
    assert result.evidence[0].metadata["retrieved_at"]
    message["annotations"] = [{"url_citation": {"url": "javascript:alert(1)"}}]
    assert not (
        await tool.run(
            WebSearchArguments(query="最新指标"), ToolContext(run_id="web", agent_name="researcher")
        )
    ).success


@pytest.mark.asyncio
async def test_tool_records_survive_workflow_checkpoints_and_sqlite_snapshot() -> None:
    container = make_test_container(
        scenario=GatewayScenario(initial_task_count=2, linear_dependencies=True,
                                 task_capabilities={"p1-t1": ["calculator"]})
    )
    registry = ToolRegistry()
    registry.register(CalculatorTool())

    def handler(request):
        payload = json.loads(request.content)
        choice = payload.get("tool_choice")
        return _response(
            _call("calculator", {"expression": "1+1"})
            if isinstance(choice, dict)
            else {"content": "done"}
        )

    runtime = ToolRuntime(
        registry=registry,
        client=_client(handler),
        model="test/model",
        event_sink=container.store.append_event,
    )
    container.run_service.workflow.tool_runtime = runtime
    run = await container.run_service.execute_and_wait("1+1=?")
    assert run.status is RunStatus.COMPLETED
    assert len(run.tool_calls) == run.metrics.tool_calls == 1
    assert run.tool_calls[0].task_id == "p1-t1"
    assert run.tool_calls[0].summary == "2"
    reloaded = await container.store.get(run.id)
    assert reloaded.tool_calls == run.tool_calls
    assert any(event.event_type is RunEventType.TOOL_SUCCEEDED for event in reloaded.events)


@pytest.mark.asyncio
async def test_required_tool_failure_cannot_produce_successful_research() -> None:
    container = make_test_container(scenario=GatewayScenario(
        initial_task_count=2,
        task_capabilities={f"p{version}-t{index}": ["calculator"]
                           for version in (1, 2, 3) for index in (1, 2)},
    ))
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    runtime = ToolRuntime(
        registry=registry,
        client=_client(lambda _: _response({"content": "I decline to calculate"})),
        model="test/model",
        event_sink=container.store.append_event,
    )
    container.run_service.workflow.tool_runtime = runtime
    run = await container.run_service.execute_and_wait("1+1=?")
    assert run.status is RunStatus.FAILED
    assert run.research_results == []
    assert any("required capabilities" in error.message for error in run.errors)


@pytest.mark.asyncio
async def test_navigation_link_reaches_report_without_persisting_route_numbers() -> None:
    container = make_test_container(
        scenario=GatewayScenario(initial_task_count=2, linear_dependencies=True,
                                 task_capabilities={"p1-t1": ["map_route"]})
    )
    registry = ToolRegistry()
    registry.register(AmapRouteTool("amap-test-key", transport=httpx.MockTransport(_amap_handler)))

    def handler(request):
        payload = json.loads(request.content)
        choice = payload.get("tool_choice")
        return _response(
            _call(
                "map_route",
                {
                    "city": "北京",
                    "origin": "起点",
                    "destination": "终点",
                    "mode": "walking",
                },
            )
            if isinstance(choice, dict)
            else {"content": "done"}
        )

    container.run_service.workflow.tool_runtime = ToolRuntime(
        registry=registry,
        client=_client(handler),
        model="test/model",
        event_sink=container.store.append_event,
    )
    run = await container.run_service.execute_and_wait("北京两日游，提供步行导航")
    assert run.status is RunStatus.COMPLETED
    assert len(run.tool_calls) == 1
    assert run.tool_calls[0].navigation_url in run.report
    persisted = (await container.store.get(run.id)).model_dump_json()
    assert "distance_m" not in persisted and "duration_s" not in persisted
    # Whole-snapshot substring checks are flaky: UUIDs and timestamps can
    # legitimately contain the same digits as the route test fixture.
    assert "12345" not in run.report and "6789" not in run.report
    assert run.tool_calls[0].arguments == {"mode": "walking"}
    assert "amap-test-key" not in persisted
