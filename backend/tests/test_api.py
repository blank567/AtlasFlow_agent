from atlasflow.main import create_app
from fastapi.testclient import TestClient

from backend.tests.fakes import make_test_container, make_test_settings


def test_health_tools_and_document_ingestion() -> None:
    client = TestClient(
        create_app(settings=make_test_settings(), container=make_test_container())
    )

    health = client.get("/api/v1/health")
    tools = client.get("/api/v1/tools")
    document = client.post(
        "/api/v1/documents",
        json={"title": "Interview Notes", "content": "Agent systems need observable tools."},
    )

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert {tool["name"] for tool in tools.json()} >= {"knowledge_search", "web_search"}
    assert document.status_code == 200
    assert document.json()["chunks_created"] == 1
