import json
from typing import Any

import httpx
import pytest
from atlasflow.agents.contracts import (
    CritiqueRoute,
    ResearchPlan,
    ResearchResult,
    ResearchTask,
    ReviewContext,
    RunPolicy,
)
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
async def test_chat_preserves_only_real_provider_usage_and_identifiers() -> None:
    request_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"x-request-id": "req-openrouter-1"},
            json={
                "id": "gen-openrouter-1",
                "model": "actual/provider-model",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 7,
                    "total_tokens": 19,
                    "cost": 0.00125,
                },
            },
        )

    client = OpenRouterClient(
        api_key="secret-test-key",
        base_url="https://openrouter.test/api/v1",
        transport=httpx.MockTransport(handler),
    )

    message = await client.chat(
        model="requested/router-model",
        messages=[{"role": "user", "content": "hello"}],
    )

    metadata = message["_atlasflow_provider"]
    assert metadata["requested_model"] == "requested/router-model"
    assert metadata["model"] == "actual/provider-model"
    assert metadata["request_id"] == "req-openrouter-1"
    assert metadata["generation_id"] == "gen-openrouter-1"
    assert metadata["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 7,
        "total_tokens": 19,
        "cost": 0.00125,
    }
    assert client.recent_calls[-1].usage is not None
    assert client.recent_calls[-1].usage.total_tokens == 19
    assert request_payload["usage"] == {"include": True}


@pytest.mark.asyncio
async def test_missing_provider_usage_is_not_estimated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "a long response"}}]},
        )

    client = OpenRouterClient(
        api_key="secret-test-key",
        base_url="https://openrouter.test/api/v1",
        transport=httpx.MockTransport(handler),
    )

    message = await client.chat(
        model="requested/model",
        messages=[{"role": "user", "content": "a long prompt"}],
    )

    metadata = message["_atlasflow_provider"]
    assert metadata["requested_model"] == "requested/model"
    assert "model" not in metadata
    assert "request_id" not in metadata
    assert "generation_id" not in metadata
    assert "usage" not in metadata
    assert client.recent_calls[-1].usage is None


@pytest.mark.asyncio
async def test_http_success_without_chat_message_is_recorded_as_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"choices": []})

    client = OpenRouterClient(
        api_key="secret-test-key",
        base_url="https://openrouter.test/api/v1",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderRequestError, match="contains no message"):
        await client.chat(
            model="requested/model",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert len(client.recent_calls) == 1
    assert client.recent_calls[0].status == "failed"
    assert client.recent_calls[0].error_type == "InvalidChatResponse"


@pytest.mark.asyncio
async def test_planner_retries_a_malformed_structured_response() -> None:
    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0
            self.requests: list[dict[str, Any]] = []

        async def chat(self, **kwargs: Any) -> dict[str, str]:
            self.calls += 1
            self.requests.append(kwargs)
            if self.calls == 1:
                return {"content": "not-json"}
            return {
                "content": json.dumps(
                    {
                        "rationale": "先分析现状，再形成建议",
                        "tasks": [
                            {
                                "task_id": "T1",
                                "title": "分析现状",
                                "objective": "识别问题的关键约束",
                                "success_criteria": ["列出关键约束"],
                                "priority": 1,
                                "dependencies": [],
                            },
                            {
                                "task_id": "T2",
                                "title": "形成建议",
                                "objective": "基于约束给出建议",
                                "success_criteria": ["给出可执行建议"],
                                "priority": 2,
                                "dependencies": ["T1"],
                            },
                        ],
                    },
                    ensure_ascii=False,
                )
            }

    client = FlakyClient()
    gateway = OpenRouterModelGateway(client, "openrouter/free")  # type: ignore[arg-type]

    plan = await gateway.create_plan("测试问题")

    assert plan.plan_version == 1
    assert plan.task_ids == ("T1", "T2")
    assert client.calls == 2
    assert all(
        request["response_format"]["json_schema"]["strict"] is True
        for request in client.requests
    )
    task_schema = client.requests[-1]["response_format"]["json_schema"]["schema"]
    assert task_schema["properties"]["tasks"]["minItems"] == 2
    assert task_schema["properties"]["tasks"]["maxItems"] == 5


@pytest.mark.asyncio
async def test_planner_policy_sets_an_exact_initial_task_schema() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.request: dict[str, Any] = {}

        async def chat(self, **kwargs: Any) -> dict[str, str]:
            self.request = kwargs
            return {
                "content": json.dumps(
                    {
                        "rationale": "按策略生成两个初始任务",
                        "tasks": [
                            {
                                "task_id": f"T{index}",
                                "title": f"任务 {index}",
                                "objective": f"完成研究维度 {index}",
                                "success_criteria": [f"完成维度 {index}"],
                                "priority": index,
                                "dependencies": [],
                            }
                            for index in (1, 2)
                        ],
                    },
                    ensure_ascii=False,
                )
            }

    client = RecordingClient()
    gateway = OpenRouterModelGateway(client, "openrouter/free")  # type: ignore[arg-type]

    plan = await gateway.create_plan(
        "黄山风景介绍",
        policy=RunPolicy(initial_task_count=2),
    )

    schema = client.request["response_format"]["json_schema"]["schema"]
    assert plan.task_ids == ("T1", "T2")
    assert schema["properties"]["tasks"]["minItems"] == 2
    assert schema["properties"]["tasks"]["maxItems"] == 2


@pytest.mark.asyncio
async def test_critic_repairs_a_task_count_based_replan_on_second_review() -> None:
    class RepairingClient:
        def __init__(self) -> None:
            self.calls = 0
            self.requests: list[dict[str, Any]] = []

        async def chat(self, **kwargs: Any) -> dict[str, str]:
            self.calls += 1
            self.requests.append(kwargs)
            if self.calls == 1:
                payload = {
                    "decision": "replan",
                    "rationale": "当前计划有三个任务，超过初始两个任务",
                    "replan_reason": "invalid_decomposition",
                    "issues": [
                        {
                            "severity": "warning",
                            "code": "task_count_mismatch",
                            "message": "当前有三个任务",
                            "task_id": None,
                            "recommendation": "重新规划为两个任务",
                        }
                    ],
                    "supplemental_tasks": [],
                }
            else:
                payload = {
                    "decision": "accept",
                    "rationale": "两个初始任务和一个补充任务均已完成",
                    "replan_reason": None,
                    "issues": [],
                    "supplemental_tasks": [],
                }
            return {"content": json.dumps(payload, ensure_ascii=False)}

    tasks = [
        ResearchTask(
            task_id=f"T{index}",
            title=f"任务 {index}",
            objective=f"完成研究维度 {index}",
            success_criteria=[f"完成维度 {index}"],
            priority=min(index, 5),
            dependencies=[],
            plan_version=1,
        )
        for index in (1, 2, 3)
    ]
    plan = ResearchPlan(plan_version=1, rationale="2+1 任务计划", tasks=tasks)
    results = [
        ResearchResult(
            task_id=task.task_id,
            plan_version=1,
            summary=f"{task.task_id} 完成",
            findings=[f"{task.task_id} 发现"],
            confidence=0.9,
        )
        for task in tasks
    ]
    context = ReviewContext(
        review_round=2,
        plan_version=1,
        plan_revision=1,
        initial_task_ids=["T1", "T2"],
        supplemental_task_ids=["T3"],
        completed_task_ids=["T1", "T2", "T3"],
        failed_task_ids=[],
        supplement_rounds_used=1,
        supplement_rounds_remaining=0,
        current_task_count=3,
        expected_task_count=3,
        policy=RunPolicy(
            initial_task_count=2,
            required_supplement_rounds=1,
            supplement_task_count=1,
        ),
    )
    client = RepairingClient()
    gateway = OpenRouterModelGateway(client, "openrouter/free")  # type: ignore[arg-type]

    decision = await gateway.review_research(
        "黄山风景介绍",
        plan,
        results,
        context=context,
    )

    assert decision.decision is CritiqueRoute.ACCEPT
    assert client.calls == 2
    assert "第 2 次审查" in client.requests[0]["messages"][1]["content"]
    assert "本轮不得选择 supplement" in client.requests[0]["messages"][0]["content"]
    repair_prompt = client.requests[1]["messages"][-1]["content"]
    assert "上次输出未通过格式或契约校验" in repair_prompt
    assert "replan decisions require a critical issue" in repair_prompt
