from __future__ import annotations

import json
import time

from atlasflow.agents.contracts import CritiqueRoute
from atlasflow.main import create_app
from fastapi.testclient import TestClient

from backend.tests.fakes import (
    FakeModelGateway,
    GatewayScenario,
    make_test_container,
    make_test_settings,
)


def test_health_tools_and_document_ingestion() -> None:
    with TestClient(
        create_app(settings=make_test_settings(), container=make_test_container())
    ) as client:
        health = client.get("/api/v1/health")
        tools = client.get("/api/v1/tools")
        document = client.post(
            "/api/v1/documents",
            json={
                "title": "Interview Notes",
                "content": "Agent systems need observable tools.",
            },
        )

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert {tool["name"] for tool in tools.json()} >= {"knowledge_search", "web_search"}
    assert document.status_code == 200
    assert document.json()["chunks_created"] == 1


def test_knowledge_api_starts_empty_and_explains_hybrid_search() -> None:
    app = create_app(settings=make_test_settings(), container=make_test_container())
    with TestClient(app) as client:
        spaces = client.get("/api/v1/knowledge/spaces")
        empty_documents = client.get("/api/v1/knowledge/spaces/user-default/documents")
        ingested = client.post(
            "/api/v1/documents",
            json={
                "title": "Vector Notes",
                "source_id": "vector-notes",
                "content": "向量检索用于查找语义相近的知识片段。",
                "metadata": {"language": "zh", "tags": ["rag"]},
            },
        )
        search = client.post(
            "/api/v1/knowledge/search/debug",
            json={"space": "user-default", "query": "什么是向量检索？"},
        )
        archived = client.post(
            f"/api/v1/knowledge/documents/{ingested.json()['document_id']}/archive"
        )
        after_archive = client.post(
            "/api/v1/knowledge/search",
            json={"space": "user-default", "query": "什么是向量检索？"},
        )

    assert spaces.status_code == 200
    assert [item["slug"] for item in spaces.json()] == ["user-default"]
    assert empty_documents.json() == []
    assert search.status_code == 200
    assert search.json()["hits"]
    assert search.json()["trace"]["lexical_candidates"] == 1
    assert "rerank_applied" in search.json()["trace"]
    assert archived.json()["status"] == "archived"
    assert after_archive.json()["decision"]["status"] == "insufficient"


def test_capability_catalog_and_invalid_approval_edit() -> None:
    with TestClient(
        create_app(settings=make_test_settings(), container=make_test_container())
    ) as client:
        catalog = client.get("/api/v1/capabilities")
        assert catalog.status_code == 200
        assert {item["id"] for item in catalog.json()} >= {
            "calculator",
            "web_search",
            "map_route",
            "knowledge_search",
        }
        created = client.post("/api/v1/runs", json={"query": "解释概念", "auto_approve": False})
        run_id = created.json()["id"]
        run = _wait_for_status(client, run_id, {"waiting_approval"})
        edited = run["plan"]
        edited["tasks"][0]["required_capabilities"] = ["unknown_capability"]
        response = client.post(
            f"/api/v1/runs/{run_id}/approval", json={"action": "edit", "edited_plan": edited}
        )
        assert response.status_code == 422
        assert "unknown capabilities" in response.json()["detail"]
        assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "waiting_approval"


def _wait_for_status(client: TestClient, run_id: str, expected: set[str]) -> dict[str, object]:
    for _ in range(300):
        response = client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in expected:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {sorted(expected)}")


def test_run_api_and_sse_have_ordered_single_done_event() -> None:
    app = create_app(settings=make_test_settings(), container=make_test_container())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={"query": "验证完整 API 生命周期", "auto_approve": True},
        )
        assert response.status_code == 202
        run_id = response.json()["id"]

        with client.stream("GET", f"/api/v1/runs/{run_id}/events") as stream:
            assert stream.status_code == 200
            body = "".join(stream.iter_text())

        final = client.get(f"/api/v1/runs/{run_id}")

    assert final.status_code == 200
    payload = final.json()
    assert payload["status"] == "completed"
    assert payload["report"]
    assert body.count("event: done\n") == 1
    assert body.count("event: run_completed\n") == 1
    assert body.index("event: run_completed\n") < body.index("event: done\n")

    event_payloads = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: {") and '"event_type"' in line
    ]
    assert [item["sequence"] for item in event_payloads] == list(range(1, len(event_payloads) + 1))


def test_run_api_propagates_structured_policy_to_the_workflow() -> None:
    policy = {
        "initial_task_count": 2,
        "required_supplement_rounds": 1,
        "supplement_task_count": 1,
        "replan_requires_critical_issue": True,
    }
    gateway = FakeModelGateway(
        GatewayScenario(critique_routes=[CritiqueRoute.SUPPLEMENT, CritiqueRoute.ACCEPT])
    )
    app = create_app(
        settings=make_test_settings(),
        container=make_test_container(model=gateway),
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs",
            json={
                "query": "黄山风景介绍",
                "auto_approve": True,
                "policy": policy,
            },
        )
        assert created.status_code == 202
        assert created.json()["policy"] == {
            **policy,
            "max_tool_calls_per_turn": 5,
            "max_tool_calls_per_run": 20,
            "planner_allow_research": False,
        }
        final = _wait_for_status(
            client,
            created.json()["id"],
            {"completed", "failed"},
        )

    assert final["status"] == "completed"
    assert final["policy"] == {
        **policy,
        "max_tool_calls_per_turn": 5,
        "max_tool_calls_per_run": 20,
        "planner_allow_research": False,
    }
    assert final["plan_lineage"]["base_plan"]["tasks"][0]["task_id"] == "p1-t1"
    assert len(final["plan_lineage"]["base_plan"]["tasks"]) == 2
    assert len(final["plan_lineage"]["supplements"][0]["tasks"]) == 1
    assert len(final["plan"]["tasks"]) == 3
    assert final["review_contexts"][1]["review_round"] == 2
    assert final["review_contexts"][1]["current_task_count"] == 3
    assert final["review_contexts"][1]["expected_task_count"] == 3
    assert gateway.plan_policies[0] is not None
    assert gateway.plan_policies[0].model_dump(mode="json") == {
        **policy,
        "max_tool_calls_per_turn": 5,
        "max_tool_calls_per_run": 20,
        "planner_allow_research": False,
    }
    assert gateway.review_contexts[-1].current_task_count == 3
    assert gateway.review_contexts[-1].expected_task_count == 3


def test_manual_approval_api_and_conflict_semantics() -> None:
    app = create_app(settings=make_test_settings(), container=make_test_container())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs",
            json={"query": "验证人工审批接口", "auto_approve": False},
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        waiting = _wait_for_status(client, run_id, {"waiting_approval"})
        assert waiting["plan"]

        approved = client.post(f"/api/v1/runs/{run_id}/approval", json={"action": "approve"})
        assert approved.status_code == 202
        assert approved.json()["status"] == "running"

        final = _wait_for_status(
            client,
            run_id,
            {"completed", "completed_with_warnings", "failed", "cancelled"},
        )
        duplicate = client.post(f"/api/v1/runs/{run_id}/approval", json={"action": "approve"})
        missing = client.post("/api/v1/runs/not-found/approval", json={"action": "approve"})

    assert final["status"] == "completed"
    assert duplicate.status_code == 409
    assert missing.status_code == 404
