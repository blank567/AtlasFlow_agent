import json
from typing import Any

import httpx
import pytest
from atlasflow.agents.gateway import OpenRouterModelGateway
from atlasflow.providers.openrouter import OpenRouterClient, ProviderRequestError


@pytest.mark.asyncio
async def test_openrouter_contracts_and_embedding_order() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert request.headers["authorization"] == "Bearer secret-test-key"
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                },
            )
        if request.url.path.endswith("/rerank"):
            return httpx.Response(
                200,
                json={"results": [{"index": 1, "relevance_score": 0.93}]},
            )
        return httpx.Response(404)

    client = OpenRouterClient(
        api_key="secret-test-key",
        base_url="https://openrouter.test/api/v1/chat/completions",
        transport=httpx.MockTransport(handler),
    )

    message = await client.chat(
        model="test/model", messages=[{"role": "user", "content": "hello"}]
    )
    embeddings = await client.embed(
        model="test/embed", texts=["first", "second"], input_type="search_document"
    )
    reranked = await client.rerank(
        model="test/rerank", query="query", documents=["a", "b"], top_n=1
    )

    assert message["content"] == "ok"
    assert embeddings == [[1.0, 0.0], [0.0, 1.0]]
    assert reranked[0].index == 1
    assert seen_paths == [
        "/api/v1/chat/completions",
        "/api/v1/embeddings",
        "/api/v1/rerank",
    ]


@pytest.mark.asyncio
async def test_auth_failure_is_not_retried_or_leaked() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "secret-test-key rejected"})

    client = OpenRouterClient(
        api_key="secret-test-key",
        base_url="https://openrouter.test/api/v1",
        max_retries=3,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderRequestError) as error:
        await client.chat(
            model="test/model", messages=[{"role": "user", "content": "hello"}]
        )

    assert calls == 1
    assert "secret-test-key" not in str(error.value)


@pytest.mark.asyncio
async def test_rate_limit_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenRouterClient(
        api_key="secret-test-key",
        base_url="https://openrouter.test/api/v1",
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )

    result = await client.chat(
        model="test/model", messages=[{"role": "user", "content": "hello"}]
    )

    assert result["content"] == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_structured_output_uses_supported_routes_and_healing() -> None:
    request_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_payload.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = OpenRouterClient(
        api_key="secret-test-key",
        base_url="https://openrouter.test/api/v1",
        transport=httpx.MockTransport(handler),
    )
    await client.chat(
        model="openrouter/free",
        messages=[{"role": "user", "content": "return json"}],
        response_format={"type": "json_schema", "json_schema": {"name": "test"}},
    )

    assert request_payload["provider"] == {"require_parameters": True}
    assert request_payload["plugins"] == [{"id": "response-healing"}]


@pytest.mark.asyncio
async def test_planner_retries_a_malformed_structured_response() -> None:
    class FlakyClient:
        calls = 0

        async def chat(self, **kwargs: Any) -> dict[str, str]:
            self.calls += 1
            if self.calls == 1:
                return {"content": "not-json"}
            return {"content": '{"steps":["检索","分析","报告"]}'}

    client = FlakyClient()
    gateway = OpenRouterModelGateway(client, "openrouter/free")  # type: ignore[arg-type]

    assert await gateway.create_plan("测试问题") == ["检索", "分析", "报告"]
    assert client.calls == 2
