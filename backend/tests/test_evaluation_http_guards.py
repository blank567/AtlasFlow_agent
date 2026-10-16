"""Actual adapter sending boundaries share the experiment cap, including retries."""
import httpx
import pytest
from atlasflow.evaluation.budget import (
    EvaluationBudget,
    EvaluationBudgetExceeded,
    evaluation_budget_context,
)
from atlasflow.evaluation.contracts import EvaluationBudgetSpec
from atlasflow.providers.openrouter import OpenRouterClient
from atlasflow.tools.base import ToolContext
from atlasflow.tools.map_route import AmapRouteTool
from atlasflow.tools.weather_forecast import WeatherForecastArguments, WeatherForecastTool
from atlasflow.tools.web_fetch import WebFetchArguments, WebFetchTool


def budget():
    return EvaluationBudget(EvaluationBudgetSpec(max_http_attempts=1))


@pytest.mark.asyncio
async def test_openrouter_retry_cannot_send_second_attempt():
    sent = []
    client = OpenRouterClient(api_key="fake", base_url="https://example.test/v1",
                              max_retries=2, transport=httpx.MockTransport(
                                  lambda request: sent.append(request) or httpx.Response(429)))
    cap = budget()
    with evaluation_budget_context(cap), pytest.raises(EvaluationBudgetExceeded):
        await client.chat(model="test/model", messages=[{"role": "user", "content": "test"}])
    assert len(sent) == 1
    assert cap.ledger()["by_kind"] == {"model": 1}


@pytest.mark.asyncio
async def test_amap_internal_retry_is_counted():
    sent = []
    tool = AmapRouteTool(api_key="fake")
    cap = budget()
    async with httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(
        lambda request: sent.append(request) or httpx.Response(429))) as client:
        with evaluation_budget_context(cap), pytest.raises(EvaluationBudgetExceeded):
            await tool._get_json(client, "/v3/place/text", {"keywords": "place"})
    assert len(sent) == 1
    assert cap.ledger()["by_kind"] == {"map": 1}


@pytest.mark.asyncio
async def test_weather_geocode_and_forecast_share_cap():
    sent = []
    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={"results": [{"name": "Beijing", "country": "China",
            "country_code": "CN", "admin1": "Beijing", "latitude": 39.9, "longitude": 116.4}]})
    tool = WeatherForecastTool(transport=httpx.MockTransport(respond))
    cap = budget()
    with evaluation_budget_context(cap), pytest.raises(EvaluationBudgetExceeded):
        await tool.run(WeatherForecastArguments(city="Beijing", days=1),
                       ToolContext(run_id="evaluation", agent_name="researcher"))
    assert len(sent) == 1
    assert cap.ledger()["by_kind"] == {"weather": 1}


@pytest.mark.asyncio
async def test_fetch_redirect_is_a_separate_business_http_attempt():
    sent = []
    async def dns(host, port):
        return ["93.184.215.14"]
    tool = WebFetchTool(resolver=dns, transport=httpx.MockTransport(
        lambda request: sent.append(request) or httpx.Response(302, headers={"location": "/next"})))
    cap = budget()
    with evaluation_budget_context(cap), pytest.raises(EvaluationBudgetExceeded):
        await tool.run(WebFetchArguments(url="https://example.org/page"),
                       ToolContext(run_id="evaluation", agent_name="researcher"))
    assert len(sent) == 1
    assert cap.ledger()["by_kind"] == {"web_fetch": 1}
