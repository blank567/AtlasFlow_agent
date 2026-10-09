from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.tool_runtime import RequiredToolError, ToolRuntime
from atlasflow.agents.workflow import ResearchWorkflow
from atlasflow.knowledge.domain import RetrievalDecision, RetrievalTrace, SearchResult
from atlasflow.main import create_app
from atlasflow.providers.openrouter import OpenRouterClient
from atlasflow.tools import ToolRegistry
from atlasflow.tools.knowledge_search import KnowledgeSearchTool
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.tests.fakes import FakeModelGateway, make_test_container, make_test_settings


class RecordingKnowledgePlatform:
    def __init__(self) -> None:
        self.spaces: list[str | None] = []

    async def search(self, query: str, *, space: str | None, **_: Any) -> SearchResult:
        self.spaces.append(space)
        return SearchResult(
            query=query,
            decision=RetrievalDecision(status="insufficient", evidence_count=0, coverage=0),
            trace=RetrievalTrace(profile_id="test", generation_id=None),
        )


def _runtime(platform: RecordingKnowledgePlatform, handler) -> ToolRuntime:
    registry = ToolRegistry()
    registry.register(KnowledgeSearchTool(platform))  # type: ignore[arg-type]

    async def emit(_run_id, _event) -> None:
        return None

    return ToolRuntime(
        registry=registry,
        client=OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.test/api/v1",
            max_retries=0,
            transport=httpx.MockTransport(handler),
        ),
        model="test/model",
        event_sink=emit,
    )


def _tool_response(arguments: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-knowledge",
                                "type": "function",
                                "function": {
                                    "name": "knowledge_search",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    }
                }
            ]
        },
    )


def test_run_policy_requires_a_real_selected_space() -> None:
    with pytest.raises(ValidationError, match="requires knowledge_space"):
        RunPolicy(knowledge_mode="selected")
    with pytest.raises(ValidationError, match="only valid for selected"):
        RunPolicy(knowledge_mode="off", knowledge_space="user-default")
    with pytest.raises(ValidationError):
        RunPolicy(knowledge_mode="selected", knowledge_space="../escape")


@pytest.mark.asyncio
async def test_selected_run_overrides_model_made_space() -> None:
    platform = RecordingKnowledgePlatform()
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            parameters = payload["tools"][0]["function"]["parameters"]
            assert "space" not in parameters["properties"]
            return _tool_response({"query": "LoRA", "space": "default"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "done"}}]})

    runtime = _runtime(platform, handler)
    result = await runtime.gather(
        run_id="selected-space",
        agent="researcher",
        prompt="检索 LoRA",
        policy=RunPolicy(knowledge_mode="selected", knowledge_space="llm-engineering-v1"),
        required_capabilities=["knowledge_search"],
    )
    assert platform.spaces == ["llm-engineering-v1"]
    assert result.records[0].arguments["space"] == "llm-engineering-v1"
    assert result.records[0].success


@pytest.mark.asyncio
async def test_default_run_ignores_invalid_model_space_alias() -> None:
    platform = RecordingKnowledgePlatform()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return _tool_response({"query": "LoRA", "space": "default"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "done"}}]})

    result = await _runtime(platform, handler).gather(
        run_id="legacy-default",
        agent="researcher",
        prompt="检索 LoRA",
        policy=RunPolicy(),
        required_capabilities=["knowledge_search"],
    )
    assert platform.spaces == [None]
    assert "space" not in result.records[0].arguments


@pytest.mark.asyncio
async def test_off_run_does_not_advertise_or_execute_knowledge_search() -> None:
    platform = RecordingKnowledgePlatform()

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled knowledge tool must not call the model")

    runtime = _runtime(platform, handler)
    result = await runtime.gather(
        run_id="knowledge-off", agent="researcher", prompt="解释概念",
        policy=RunPolicy(knowledge_mode="off"),
    )
    assert result.records == []
    assert result.model_calls == 0
    with pytest.raises(RequiredToolError, match="knowledge_search"):
        await runtime.gather(
            run_id="knowledge-off-required", agent="researcher", prompt="解释概念",
            policy=RunPolicy(knowledge_mode="off"),
            required_capabilities=["knowledge_search"],
        )
    assert platform.spaces == []


def test_planner_catalog_obeys_knowledge_mode() -> None:
    platform = RecordingKnowledgePlatform()
    runtime = _runtime(platform, lambda _: pytest.fail("no request expected"))

    async def emit(_run_id, _event) -> None:
        return None

    workflow = ResearchWorkflow(
        model=FakeModelGateway(), event_sink=emit, tool_runtime=runtime,
    )
    assert "knowledge_search" in {
        item["id"] for item in workflow._capability_catalog(RunPolicy())
    }
    assert "knowledge_search" not in {
        item["id"] for item in workflow._capability_catalog(RunPolicy(knowledge_mode="off"))
    }


def test_create_run_validates_selected_space_before_starting() -> None:
    with TestClient(
        create_app(settings=make_test_settings(), container=make_test_container())
    ) as client:
        invalid = client.post(
            "/api/v1/runs",
            json={
                "query": "解释 LoRA 原理",
                "policy": {"knowledge_mode": "selected", "knowledge_space": "not-found"},
            },
        )
        assert invalid.status_code == 422
        valid = client.post(
            "/api/v1/runs",
            json={
                "query": "解释 LoRA 原理",
                "policy": {"knowledge_mode": "selected", "knowledge_space": "user-default"},
            },
        )
        assert valid.status_code == 202
        assert valid.json()["policy"]["knowledge_space"] == "user-default"
