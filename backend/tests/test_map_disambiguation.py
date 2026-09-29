"""Structured location intent, conservative matching, and private failure recovery."""

import json

import httpx
import pytest
from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.tool_runtime import RequiredToolError
from atlasflow.observability import _sanitize_trace_outputs
from atlasflow.schemas import ToolCallRecord
from atlasflow.tools.base import ToolContext
from atlasflow.tools.map_itinerary import MapItineraryArguments, MapItineraryTool
from atlasflow.tools.map_route import AmapPlace, AmapRouteArguments, AmapRouteTool
from atlasflow.tools.registry import ToolRegistry
from pydantic import ValidationError

from backend.tests.test_tool_runtime import _amap_handler, _call, _response, _runtime

CONTEXT = ToolContext(run_id="places", agent_name="researcher")


def poi(name="同名馆", **kwargs):
    return {"id": "poi-1", "name": name, "location": "116.4,39.9", **kwargs}


async def resolve(rows, place, *, city="北京市"):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "1", "pois": rows})

    tool = AmapRouteTool("test-key", transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=tool._transport, base_url=tool.base_url) as client:
        selected = await tool._resolve(client, city, place)
    return selected, requests


@pytest.mark.asyncio
async def test_district_and_address_disambiguate_identical_names():
    rows = [
        poi(adname="海淀区", address="展览路1号"),
        poi(id="poi-2", adname="东城区", address="展览路1号"),
        poi(id="poi-3", adname="东城区", address="展览路11号"),
    ]
    selected, requests = await resolve(
        rows, AmapPlace(name="同名馆", district="东城区", address="北京市东城区展览路1号")
    )
    assert selected["id"] == "poi-2"
    assert requests[0].url.params["page_size"] == "25"
    assert requests[0].url.params["city_limit"] == "true"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["天坛公园", "天坛公园东门", "北京市天坛公园"])
async def test_explicit_entrance_is_not_confused_with_parent_or_other_gate(name):
    rows = [
        poi("天坛公园", id="parent"),
        poi("天坛公园-西门", id="west"),
        poi("天坛公园(东门)", id="east"),
    ]
    selected, requests = await resolve(rows, AmapPlace(name=name, entrance="东门"))
    assert selected["id"] == "east"
    assert requests[0].url.params["keywords"].count("东门") == 1


@pytest.mark.asyncio
async def test_city_prefix_and_duplicate_provider_rows_do_not_create_false_ambiguity():
    selected, _ = await resolve([poi("同名馆"), poi("同名馆")], "北京市同名馆")
    assert selected["id"] == "poi-1"


@pytest.mark.asyncio
async def test_short_city_input_accepts_full_administrative_prefix_without_stripping_brand():
    selected, _ = await resolve([poi("同名馆")], "北京市同名馆", city="北京")
    assert selected["id"] == "poi-1"
    assert AmapRouteTool._local_name("北京大学", "北京", None) == "北京大学"


@pytest.mark.asyncio
async def test_legacy_full_address_requires_exact_provider_address():
    selected, _ = await resolve([poi(address="东城区展览路1号")], "北京市东城区展览路1号")
    assert selected["id"] == "poi-1"


@pytest.mark.asyncio
async def test_exact_match_beyond_old_three_candidate_window():
    rows = [poi(f"目标馆-{index}号停车场", id=str(index)) for index in range(4)]
    selected, _ = await resolve([*rows, poi("目标馆", id="target")], "目标馆")
    assert selected["id"] == "target"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows,place,code",
    [
        ([poi("天坛公园-东门"), poi("天坛公园-西门", id="west")], "天坛公园", "NO_MATCH"),
        ([poi("同名馆-停车场")], "同名馆", "NO_MATCH"),
        ([poi(adname="海淀区")], AmapPlace(name="同名馆", district="东城区"), "NO_MATCH"),
        ([poi(address="展览路11号")], AmapPlace(name="同名馆", address="展览路1号"), "NO_MATCH"),
        ([poi(location="nan,39")], "同名馆", "NOT_FOUND"),
        ([poi(location="181,39")], "同名馆", "NOT_FOUND"),
        ([poi()], "甲" * 81, "QUERY_TOO_LONG"),
    ],
)
async def test_conflicting_or_unverifiable_places_never_pick_first(rows, place, code):
    tool = AmapRouteTool(
        "secret",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"status": "1", "pois": rows})
        ),
    )
    result = await tool.run(
        AmapRouteArguments(city="北京", origin=place, destination="终点", mode="walking"), CONTEXT
    )
    assert not result.success and code in result.error and "起点" in result.error
    assert not result.retryable and result.data == {}
    assert "secret" not in result.model_dump_json()
    # Only app-generated instructions leave the tool; provider candidate records do not.
    assert "poi-1" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_destination_error_and_multi_stop_error_identify_editable_input():
    def handler(request):
        if request.url.params.get("keywords") == "同名馆":
            return httpx.Response(200, json={"status": "1", "pois": [poi("无关地点")]})
        return _amap_handler(request)

    tool = MapItineraryTool("amap-test-key", transport=httpx.MockTransport(handler))
    result = await tool.run(
        MapItineraryArguments(
            city="北京",
            stops=[{"name": "起点"}, {"name": "中间点"}, {"name": "同名馆"}],
            mode="walking",
        ),
        CONTEXT,
    )
    assert not result.success and "预解析第 3 站" in result.error
    assert len(result.data["navigation_urls"]) == 0
    assert "poi-1" not in json.dumps(_sanitize_trace_outputs(result))


def test_structured_schema_and_legacy_compatibility():
    schema = AmapRouteArguments.model_json_schema()
    assert "AmapPlace" in schema["$defs"]
    assert set(schema["$defs"]["AmapPlace"]["properties"]) == {
        "name",
        "district",
        "address",
        "entrance",
    }
    legacy = {"city": "北京", "origin": "起点", "destination": "终点", "mode": "walking"}
    structured = {**legacy, "origin": {"name": "起点"}}
    assert AmapRouteTool.failure_scope(legacy) == AmapRouteTool.failure_scope(structured)
    assert AmapRouteTool.failure_scope({"mode": "walking"}) is None
    for stops in (["起点", {"name": "起点"}], [" ", "终点"]):
        with pytest.raises(ValidationError):
            MapItineraryArguments(city="北京", stops=stops, mode="walking")
    with pytest.raises(ValidationError):
        AmapPlace(name="起点", poi_id="invented")


@pytest.mark.asyncio
async def test_same_failed_map_request_is_suppressed_across_stages_but_refinement_allowed():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v5/place/text" and "同名馆" in request.url.params["keywords"]:
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "pois": [poi(adname="东城区"), poi(id="other", adname="海淀区")],
                },
            )
        return _amap_handler(request)

    registry = ToolRegistry()
    registry.register(AmapRouteTool("amap-test-key", transport=httpx.MockTransport(handler)))
    original = {"city": "北京", "origin": {"name": "同名馆", "district": "西城区"}, "destination": "终点", "mode": "walking"}
    fixed = {**original, "origin": {"name": "同名馆", "district": "东城区"}}
    replies = iter([_call("map_route", args) for args in (original, original, original, fixed)])
    runtime, events = _runtime(registry, lambda _: _response(next(replies)))
    options = {
        "run_id": "places",
        "agent": "researcher",
        "prompt": "导航",
        "policy": RunPolicy(),
        "required_capabilities": ["map_route"],
    }
    with pytest.raises(RequiredToolError) as first:
        await runtime.gather(**options)
    records = [
        ToolCallRecord.model_validate_json(row.model_dump_json())
        for row in first.value.stage.records
    ]
    assert len(records) == len(requests) == 1
    assert len(records[0].failure_scope) == 64
    assert records[0].arguments == {"mode": "walking"}
    assert "同名馆" not in records[0].model_dump_json()
    with pytest.raises(RequiredToolError) as second:
        await runtime.gather(**options, prior_calls=records)
    assert not second.value.stage.records and len(requests) == 1
    stage = await runtime.gather(**options, prior_calls=records)
    assert stage.records[0].success and len(requests) == 4
    assert sum(bool(event.data.get("suppressed")) for event in events) == 2


@pytest.mark.asyncio
async def test_model_receives_safe_hint_and_can_refine_in_same_stage():
    registry = ToolRegistry()

    def handler(request):
        if request.url.path == "/v5/place/text" and "同名馆" in request.url.params["keywords"]:
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "pois": [poi(adname="东城区"), poi(id="other", adname="海淀区")],
                },
            )
        return _amap_handler(request)

    registry.register(AmapRouteTool("amap-test-key", transport=httpx.MockTransport(handler)))
    calls = 0

    def decide(request):
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        args = {"city": "北京", "origin": {"name": "同名馆", "district": "西城区"}, "destination": "终点", "mode": "walking"}
        if calls == 2:
            hint = json.loads(payload["messages"][-1]["content"])
            assert "起点" in hint["error"] and "district" in hint["error"]
            assert hint["data"] == {} and "poi-1" not in json.dumps(payload)
            args["origin"] = {"name": "同名馆", "district": "东城区"}
        return _response(_call("map_route", args))

    runtime, _ = _runtime(registry, decide)
    stage = await runtime.gather(
        run_id="refine",
        agent="researcher",
        prompt="行程已确定在东城区同名馆出发",
        policy=RunPolicy(),
        required_capabilities=["map_route"],
    )
    assert [row.success for row in stage.records] == [False, True]
    assert calls == 2  # Never send successful raw map data to another model call.
