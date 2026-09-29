"""Highest-score official POI resolution before any itinerary route request."""

import httpx
import pytest
from atlasflow.tools.base import ToolContext
from atlasflow.tools.map_itinerary import MapItineraryArguments, MapItineraryTool
from atlasflow.tools.map_route import AmapPlace, AmapRouteTool

from backend.tests.test_map_disambiguation import poi, resolve
from backend.tests.test_tool_runtime import _amap_handler


@pytest.mark.asyncio
async def test_highest_name_score_wins_not_provider_first_row():
    selected, _ = await resolve([
        poi("北京大学燕园东门", id="campus"),
        poi("北京大学东门", id="exact"),
    ], AmapPlace(name="北京大学", entrance="东门"))
    assert selected["id"] == "exact" and selected["name"] == "北京大学东门"


@pytest.mark.asyncio
async def test_official_qualified_gate_beats_wrong_type_and_wrong_direction():
    selected, _ = await resolve([
        poi("北京大学东门(地铁站)", id="metro"),
        poi("北京大学东门-停车场", id="parking"),
        poi("北京大学西门", id="west"),
        poi("北京大学燕园(东门)", id="gate"),
    ], "北京大学东门")
    assert selected["id"] == "gate" and selected["name"] == "北京大学燕园(东门)"


@pytest.mark.asyncio
async def test_equal_scores_use_provider_order_as_requested():
    selected, _ = await resolve([poi(id="first"), poi(id="second")], "同名馆")
    assert selected["id"] == "first"


def test_numbered_or_diagonal_gate_is_not_a_matching_direction():
    tool = AmapRouteTool("test")
    assert tool._poi_score(poi("北京大学东2门"), AmapPlace(name="北京大学东1门"), "北京") is None
    assert tool._poi_score(poi("北京大学东南门"), AmapPlace(name="北京大学东门"), "北京") is None


@pytest.mark.asyncio
async def test_preflight_all_stops_once_then_route_uses_official_poi_ids():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v5/place/text":
            name = request.url.params["keywords"]
            official = "北京大学燕园(东门)" if name == "北京大学东门" else name
            return httpx.Response(200, json={"status": "1", "pois": [poi(official, id=name)]})
        assert request.url.params["origin_id"] in {"北京大学东门", "景点"}
        return _amap_handler(request)

    tool = MapItineraryTool("amap-test-key", transport=httpx.MockTransport(handler))
    result = await tool.run(MapItineraryArguments(city="北京", mode="walking", stops=["北京大学东门", "景点", "北京大学东门"]), ToolContext(run_id="preflight", agent_name="researcher"))
    assert result.success and len(result.data["navigation_urls"]) == 2
    assert [request.url.path for request in requests] == ["/v5/place/text"] * 2 + ["/v5/direction/walking"] * 2
    from urllib.parse import unquote

    assert "北京大学燕园(东门)" in unquote(result.data["navigation_urls"][0])
    assert "match_score" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_route_failure_after_preflight_keeps_only_completed_leg_links():
    routes = []

    def handler(request):
        if request.url.path.startswith("/v5/direction"):
            routes.append(request)
            if len(routes) == 2:
                return httpx.Response(200, json={"status": "1", "route": {"paths": []}})
        return _amap_handler(request)

    result = await MapItineraryTool("amap-test-key", transport=httpx.MockTransport(handler)).run(
        MapItineraryArguments(city="北京", mode="walking", stops=["起点", "景点", "酒店"]),
        ToolContext(run_id="partial", agent_name="researcher"),
    )
    assert not result.success and "第 2 段" in result.error
    assert len(result.data["navigation_urls"]) == 1
