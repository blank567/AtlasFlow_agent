from __future__ import annotations

import json
import time

from atlasflow.main import create_app
from fastapi.testclient import TestClient

from backend.tests.fakes import make_test_container, make_test_settings


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


def _wait_for_status(
    client: TestClient, run_id: str, expected: set[str]
) -> dict[str, object]:
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
    assert [item["sequence"] for item in event_payloads] == list(
        range(1, len(event_payloads) + 1)
    )


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

        approved = client.post(
            f"/api/v1/runs/{run_id}/approval", json={"action": "approve"}
        )
        assert approved.status_code == 202
        assert approved.json()["status"] == "running"

        final = _wait_for_status(
            client,
            run_id,
            {"completed", "completed_with_warnings", "failed", "cancelled"},
        )
        duplicate = client.post(
            f"/api/v1/runs/{run_id}/approval", json={"action": "approve"}
        )
        missing = client.post(
            "/api/v1/runs/not-found/approval", json={"action": "approve"}
        )

    assert final["status"] == "completed"
    assert duplicate.status_code == 409
    assert missing.status_code == 404
