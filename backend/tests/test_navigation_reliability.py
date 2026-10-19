"""Navigation regressions: bounded POI recovery, independent legs, daily coverage."""

import json
from urllib.parse import unquote

import httpx
import pytest
from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.report_skill import build_citation_catalog
from atlasflow.agents.tool_runtime import RequiredToolError
from atlasflow.schemas import ToolCallRecord
from atlasflow.tools.base import ToolContext
from atlasflow.tools.map_itinerary import MapItineraryArguments, MapItineraryTool
from atlasflow.tools.map_route import AmapPlace, AmapRouteTool, MapServiceError
from atlasflow.tools.registry import ToolRegistry
from pydantic import ValidationError

from backend.tests.test_map_disambiguation import poi
from backend.tests.test_tool_runtime import _amap_handler, _call, _response, _runtime


def _map_response(request):
    if request.url.path == "/v5/place/text":
        name = request.url.params["keywords"]
        return httpx.Response(200, json={"status": "1", "pois": [poi(name, id=name)]})
    return _amap_handler(request)


@pytest.mark.asyncio
async def test_qualified_lookup_can_retry_name_without_dropping_local_constraints():
    requests = []

    def handler(request):
        requests.append(request)
        keywords = request.url.params["keywords"]
        rows = (
            []
            if keywords != "验证场馆"
            else [poi("验证场馆", id="official", adname="海淀区", address="中关村北大街126号")]
        )
        return httpx.Response(200, json={"status": "1", "pois": rows})

    tool = AmapRouteTool("test", transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(base_url=tool.base_url, transport=tool._transport) as client:
        result = await tool._resolve(
            client,
            "北京",
            AmapPlace(name="验证场馆", district="海淀区", address="中关村北大街126号"),
        )
    assert result["id"] == "official"
    assert [request.url.params["keywords"] for request in requests] == [
        "海淀区 中关村北大街126号 验证场馆",
        "验证场馆",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [{"adname": "东城区"}, {"address": "中关村北大街126号旁"}])
async def test_broader_search_never_relaxes_district_or_exact_address(mismatch):
    requests = []

    def handler(request):
        requests.append(request)
        candidate = poi("验证场馆", adname="海淀区", address="中关村北大街126号")
        candidate.update(mismatch)
        return httpx.Response(200, json={"status": "1", "pois": [candidate]})

    tool = AmapRouteTool("test", transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(base_url=tool.base_url, transport=tool._transport) as client:
        with pytest.raises(MapServiceError, match="AMAP_POI_NO_MATCH"):
            await tool._resolve(
                client,
                "北京",
                AmapPlace(name="验证场馆", district="海淀区", address="中关村北大街126号"),
            )
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_unresolved_first_station_keeps_only_later_original_legs():
    requests = []

    def handler(request):
        requests.append(request)
        if (
            request.url.path in {"/v5/place/text", "/v3/assistant/inputtips"}
            and request.url.params["keywords"] == "未能核实的地点"
        ):
            return httpx.Response(200, json={"status": "1", "pois": [], "tips": []})
        return _map_response(request)

    tool = MapItineraryTool("amap-test-key", transport=httpx.MockTransport(handler))
    result = await tool.run(
        MapItineraryArguments(
            city="北京", mode="walking", stops=["未能核实的地点", "景点", "酒店"]
        ),
        ToolContext(run_id="partial-route", agent_name="researcher"),
    )
    assert not result.success and "POI 预解析第 1 站" in result.error
    assert result.data["verified_legs"] == [2] and result.data["total_legs"] == 2
    assert len(result.data["navigation_urls"]) == 1
    assert "仅第2段" in tool.safe_summary(result)
    routes = [request for request in requests if "/direction/" in request.url.path]
    assert len(routes) == 1 and routes[0].url.params["origin_id"] == "景点"
    # No invented leg connecting a replacement of the missing starting point.
    assert "未能核实的地点" not in unquote(result.data["navigation_urls"][0])


@pytest.mark.asyncio
async def test_unresolved_middle_station_does_not_create_a_shortcut():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.params.get("keywords") == "未能核实的地点":
            return httpx.Response(200, json={"status": "1", "pois": [], "tips": []})
        return _map_response(request)

    result = await MapItineraryTool("amap-test-key", transport=httpx.MockTransport(handler)).run(
        MapItineraryArguments(
            city="北京", mode="walking", stops=["起点", "未能核实的地点", "酒店"]
        ),
        ToolContext(run_id="no-shortcut", agent_name="researcher"),
    )
    assert not result.success and not result.data["navigation_urls"]
    assert not any("/direction/" in request.url.path for request in requests)


@pytest.mark.asyncio
async def test_failed_middle_route_does_not_discard_later_independent_leg():
    requests = []

    def handler(request):
        if "/direction/" in request.url.path:
            requests.append(request)
            if len(requests) == 2:
                return httpx.Response(200, json={"status": "1", "route": {"paths": []}})
        return _map_response(request)

    result = await MapItineraryTool("amap-test-key", transport=httpx.MockTransport(handler)).run(
        MapItineraryArguments(city="北京", mode="walking", stops=["起点", "景点", "酒店", "公园"]),
        ToolContext(run_id="independent-legs", agent_name="researcher"),
    )
    assert not result.success and result.data["verified_legs"] == [1, 3]
    assert len(result.data["navigation_urls"]) == 2 and len(requests) == 3


@pytest.mark.asyncio
async def test_multi_day_preflight_once_and_never_connects_days():
    requests = []

    def handler(request):
        requests.append(request)
        return _map_response(request)

    tool = MapItineraryTool("amap-test-key", transport=httpx.MockTransport(handler))
    arguments = MapItineraryArguments(
        city="北京",
        mode="walking",
        days=[["酒店", "景点一", "酒店"], ["酒店", "景点二"], ["酒店", "景点三"]],
    )
    result = await tool.run(arguments, ToolContext(run_id="three-days", agent_name="researcher"))
    assert result.success and result.data["day_count"] == 3
    assert result.data["verified_legs"] == [1, 2, 3, 4]
    paths = [request.url.path for request in requests]
    assert paths == ["/v5/place/text"] * 4 + ["/v5/direction/walking"] * 4
    origins = [
        request.url.params["origin_id"] for request in requests if "/direction/" in request.url.path
    ]
    assert origins == ["酒店", "景点一", "酒店", "酒店"]
    assert "3天" in tool.safe_summary(result)
    assert tool.safe_arguments(arguments.model_dump())["stop_count"] == 7
    assert (
        "match_score" not in result.model_dump_json()
        and "distance_m" not in result.model_dump_json()
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"stops": ["一站"]},
        {"stops": ["一站", "二站"], "days": [["一站", "二站"]]},
        {"days": [["一站"]]},
        {"days": [["一站", "二站"]] * 4},
        {"days": [["一站", "一站"]]},
    ],
)
def test_day_group_validation(arguments):
    with pytest.raises(ValidationError):
        MapItineraryArguments(city="北京", mode="walking", **arguments)


def test_daily_input_preserves_explicit_gate_guard_and_failure_identity():
    tool = MapItineraryTool("test")
    context = ToolContext(run_id="gate", agent_name="researcher", user_query="从北大东门出发")
    arguments = {"city": "北京", "mode": "walking", "days": [["北京大学东门", "颐和园"]]}
    assert tool.validate_context(arguments, context) is None
    assert tool.validate_context({**arguments, "days": [["北京大学医院东门", "颐和园"]]}, context)
    assert tool.failure_scope(arguments) == tool.failure_scope(
        {**arguments, "days": [[{"name": "北京大学东门"}, {"name": "颐和园"}]]}
    )


@pytest.mark.asyncio
async def test_partial_route_is_failure_but_safe_record_keeps_actual_leg_number():
    def handler(request):
        if request.url.params.get("keywords") == "未知站":
            return httpx.Response(200, json={"status": "1", "pois": [], "tips": []})
        return _map_response(request)

    registry = ToolRegistry()
    registry.register(MapItineraryTool("amap-test-key", transport=httpx.MockTransport(handler)))
    runtime, events = _runtime(
        registry,
        lambda request: _response(
            _call(
                "map_itinerary",
                {"city": "北京", "mode": "walking", "stops": ["未知站", "景点", "酒店"]},
            )
        ),
    )
    with pytest.raises(RequiredToolError) as caught:
        await runtime.gather(
            run_id="partial",
            agent="researcher",
            prompt="核验导航",
            policy=RunPolicy(),
            required_capabilities=["map_itinerary"],
        )
    record = caught.value.stage.records[0]
    assert not record.success and record.navigation_leg_indices == [2]
    assert record.summary and "仅第2段" in record.summary
    context = runtime.context_for([], [record])
    assert "未核实" in context and record.call_id in context
    assert "distance_m" not in json.dumps([event.model_dump(mode="json") for event in events])
    citation = build_citation_catalog([], [record])[0]
    assert "第 2 段" in citation.title and "未完整成功" in citation.excerpt


def test_historical_tool_records_without_leg_metadata_remain_readable():
    record = ToolCallRecord(
        tool_name="map_itinerary",
        arguments={},
        success=True,
        duration_ms=1,
        navigation_urls=["https://uri.amap.com/navigation?test=1"],
    )
    assert record.navigation_leg_indices == []
    assert "第 1 段" in build_citation_catalog([], [record])[0].title


def test_bounded_map_context_cannot_drop_partial_verification_boundary():
    runtime, _ = _runtime(ToolRegistry(), lambda request: pytest.fail("no provider call"))
    urls = [f"https://uri.amap.com/navigation?leg={index}&name=" + "x" * 650 for index in range(10)]
    record = ToolCallRecord(
        tool_name="map_itinerary",
        arguments={},
        success=False,
        duration_ms=1,
        summary="仅第2段已核验，其余路段未核实，不得声称完整",
        navigation_urls=urls,
    )
    context = runtime.context_for([], [record])
    assert "其余路段未核实" in context
    assert context.index("地图核验范围") < context.index("地图导航:")
    assert len(context) < 4500 and "部分条目未展示" in context
    for line in context.splitlines():
        if line.startswith("- 地图导航:"):
            assert any(url in line for url in urls)  # Never a sliced URL.
