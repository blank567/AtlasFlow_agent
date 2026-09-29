"""Offline contracts for the source-reading, weather and POI tools."""

import json

import httpx
import pytest
from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.tool_runtime import ToolRuntime
from atlasflow.config import Settings
from atlasflow.observability import _sanitize_trace_outputs
from atlasflow.providers.openrouter import OpenRouterClient
from atlasflow.tools.base import ToolContext
from atlasflow.tools.catalog import build_tool_registry
from atlasflow.tools.poi_details import PoiDetailsArguments, PoiDetailsTool
from atlasflow.tools.registry import ToolRegistry
from atlasflow.tools.weather_forecast import WeatherForecastArguments, WeatherForecastTool
from atlasflow.tools.web_search import OpenRouterWebSearchTool


def _context() -> ToolContext:
    return ToolContext(run_id="run", agent_name="researcher")


def _web_client(handler) -> OpenRouterClient:
    return OpenRouterClient(
        api_key="test-key",
        base_url="https://openrouter.test/api/v1",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )




@pytest.mark.asyncio
async def test_weather_forecast_is_attributed_and_credential_free() -> None:
    def handler(request):
        if request.url.path == "/v1/search":
            return httpx.Response(200, json={"results": [
                {"name": "北京", "latitude": 39.9, "longitude": 116.4,
                 "admin1": "北京市", "country": "中国"}
            ]})
        return httpx.Response(200, json={
            "timezone": "Asia/Shanghai",
            "daily": {
                "time": ["2026-09-28", "2026-09-29"],
                "temperature_2m_max": [23.5, 22.0],
                "temperature_2m_min": [15.0, 14.0],
                "precipitation_probability_max": [20, 60],
                "weather_code": [1, 61],
            },
        })

    tool = WeatherForecastTool(api_key="paid-secret", transport=httpx.MockTransport(handler))
    result = await tool.run(WeatherForecastArguments(city="北京", days=2), _context())
    assert result.success and len(result.data["forecast"]) == 2
    assert "Open-Meteo" in result.evidence[0].content
    assert "paid-secret" not in result.model_dump_json()
    assert result.evidence[0].uri.startswith("https://api.open-meteo.com/")


@pytest.mark.asyncio
async def test_weather_ambiguous_city_fails_without_forecast() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [
            {"name": "Springfield", "latitude": 39.8, "longitude": -89.6},
            {"name": "Springfield", "latitude": 37.2, "longitude": -93.2},
        ]})

    result = await WeatherForecastTool(transport=httpx.MockTransport(handler)).run(
        WeatherForecastArguments(city="Springfield"), _context()
    )
    assert not result.success and "AMBIGUOUS" in result.error and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("city", ["北京", "北京市", "北京, 中国", "Beijing, China"])
async def test_weather_beijing_variants_resolve_capital_without_retry(city: str) -> None:
    geocode_requests = []

    def handler(request):
        if request.url.path == "/v1/search":
            geocode_requests.append(request)
            return httpx.Response(200, json={"results": [
                {"name": "北京", "admin1": "北京市", "country": "中国",
                 "country_code": "CN", "feature_code": "PPL", "population": 10000,
                 "latitude": 39.8, "longitude": 116.3},
                {"name": "北京", "admin1": "北京市", "country": "中国",
                 "country_code": "CN", "feature_code": "PPLC", "population": 21000000,
                 "latitude": 39.9, "longitude": 116.4},
            ]})
        assert request.url.params["latitude"] == "39.9"
        return httpx.Response(200, json={"timezone": "Asia/Shanghai", "daily": {
            "time": ["2026-09-28"],
            "temperature_2m_max": [23],
            "temperature_2m_min": [15],
            "precipitation_probability_max": [20],
            "weather_code": [1],
        }})

    result = await WeatherForecastTool(transport=httpx.MockTransport(handler)).run(
        WeatherForecastArguments(city=city, days=1), _context()
    )
    assert result.success
    assert len(geocode_requests) == 1
    assert geocode_requests[0].url.params["name"] in {"北京", "Beijing"}
    if "China" in city or "中国" in city:
        assert geocode_requests[0].url.params["countryCode"] == "CN"


@pytest.mark.asyncio
async def test_poi_is_transient_and_never_keeps_business_data() -> None:
    def handler(request):
        assert request.url.params["show_fields"] == "business"
        return httpx.Response(200, json={"status": "1", "pois": [
            {"name": "故宫博物院", "address": "示例地址", "business": {
                "opentime_today": "08:00-17:00", "cost": "40"}}
        ]})

    tool = PoiDetailsTool("secret-amap-key", transport=httpx.MockTransport(handler))
    result = await tool.run(PoiDetailsArguments(city="北京", place="故宫博物院"), _context())
    assert result.success and result.data["business"]["opentime_today"] == "08:00-17:00"
    assert result.data["business"]["average_cost_not_ticket_price"] == "40"
    assert tool.safe_evidence(result.evidence) == []
    assert "08:00" not in tool.safe_summary(result)
    assert "secret-amap-key" not in str(_sanitize_trace_outputs(result))
    assert "08:00" not in str(_sanitize_trace_outputs(result))


@pytest.mark.asyncio
async def test_poi_runtime_stops_before_second_model_call_and_redacts_records() -> None:
    registry = ToolRegistry()
    registry.register(PoiDetailsTool(
        "secret-amap-key",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "status": "1", "pois": [{
                "name": "故宫博物院",
                "business": {"opentime_today": "08:00-17:00"},
            }],
        })),
    ))
    decisions = []

    def decide(request):
        decisions.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {
            "tool_calls": [{
                "id": "poi-call", "type": "function", "function": {
                    "name": "poi_details",
                    "arguments": json.dumps({"city": "北京", "place": "故宫博物院"}),
                },
            }],
        }}]})

    events = []

    async def emit(_run_id, event):
        events.append(event)

    runtime = ToolRuntime(
        registry=registry, client=_web_client(decide), model="test/model", event_sink=emit
    )
    stage = await runtime.gather(
        run_id="poi-run", agent="researcher", prompt="核对故宫地点",
        policy=RunPolicy(), required_capabilities=["poi_details"],
    )
    assert len(decisions) == len(stage.records) == 1
    assert not stage.evidence
    assert "08:00" not in json.dumps(
        {"records": [record.model_dump(mode="json") for record in stage.records],
         "context": stage.context,
         "events": [event.model_dump(mode="json") for event in events]},
        ensure_ascii=False,
    )


def test_catalog_registers_new_capabilities_without_planner_maps_or_poi() -> None:
    client = _web_client(lambda _: httpx.Response(500))
    registry = build_tool_registry(
        Settings(_env_file=None, amap_api_key="amap-test-key"),
        web_search=OpenRouterWebSearchTool(client, "test/model"),
    )
    assert {"web_fetch", "weather_forecast", "poi_details"} <= {
        item["name"] for item in registry.describe()
    }
    assert "poi_details" not in {item["name"] for item in registry.available_for("planner")}
    assert "web_fetch" not in {item["name"] for item in registry.available_for("planner")}
