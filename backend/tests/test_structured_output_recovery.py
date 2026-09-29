"""Bounded, separately budgeted JSON truncation and validation recovery."""

import json

import httpx
import pytest
from atlasflow.agents.gateway import OpenRouterModelGateway
from atlasflow.providers.openrouter import ProviderRequestError

from backend.tests.test_tool_runtime import _client


async def generate(responses):
    requests, diagnostics = [], []
    replies = iter(responses)

    def handler(request):
        requests.append(json.loads(request.content))
        content, finish = next(replies)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]})

    gateway = OpenRouterModelGateway(_client(handler), "test/model")
    gateway._record_json_attempt = lambda **data: diagnostics.append(data)

    def validate(payload):
        if payload.get("plan") != "valid":
            raise ValueError("plan must be valid")
        return payload

    try:
        result = await gateway._request_json(
            operation="planning", messages=[{"role": "user", "content": "plan"}],
            temperature=0.1, max_tokens=4096, schema_name="test_plan",
            schema={"type": "object", "properties": {"plan": {"type": "string"}}},
            validator=validate,
        )
    except ProviderRequestError as exc:
        result = exc
    return result, requests, diagnostics


@pytest.mark.asyncio
async def test_reported_truncation_then_one_character_gets_independent_repair():
    result, requests, diagnostics = await generate([
        ('{"plan":', "length"), ("{", "stop"), ('{"plan":"valid"}', "stop")
    ])
    assert result == {"plan": "valid"}
    assert [request["max_tokens"] for request in requests] == [4096, 8192, 8192]
    assert [item["outcome"] for item in diagnostics] == ["OUTPUT_TRUNCATED", "CONTENT_TOO_SHORT", "VALID"]
    assert all(request["response_format"]["json_schema"]["strict"] for request in requests)
    assert "CONTENT_TOO_SHORT" in requests[-1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_final_error_preserves_all_three_attempts_without_raw_content():
    result, requests, diagnostics = await generate([
        ("PRIVATE_RAW_RESPONSE", "length"), (None, "stop"), ("not json PRIVATE_RAW_RESPONSE", "stop")
    ])
    assert isinstance(result, ProviderRequestError) and len(requests) == 3
    assert all(code in str(result) for code in ("OUTPUT_TRUNCATED", "EMPTY_CONTENT", "INVALID_JSON"))
    assert "PRIVATE_RAW_RESPONSE" not in str(result) + json.dumps(diagnostics)
    assert diagnostics[1]["content_length"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("content,finish,code", [
    ('{"plan":"valid"}', "length", "OUTPUT_TRUNCATED"),
    ("invalid", "stop", "INVALID_JSON"),
    (None, "stop", "EMPTY_CONTENT"),
    ('{"plan":"wrong"}', "stop", "CONTRACT_INVALID"),
])
async def test_repeated_same_failure_stops_after_two_requests(content, finish, code):
    result, requests, diagnostics = await generate([(content, finish)] * 3)
    assert isinstance(result, ProviderRequestError) and len(requests) == 2
    assert [item["outcome"] for item in diagnostics] == [code, code]


@pytest.mark.asyncio
async def test_format_repair_does_not_consume_later_truncation_recovery():
    result, requests, _ = await generate([
        ("not json", "stop"), ('{"plan":', "length"), ('{"plan":"valid"}', "stop")
    ])
    assert result == {"plan": "valid"}
    assert [request["max_tokens"] for request in requests] == [4096, 4096, 8192]
