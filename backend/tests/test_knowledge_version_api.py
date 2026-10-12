from __future__ import annotations

from types import SimpleNamespace

from atlasflow.knowledge.api.routes import router
from fastapi import FastAPI
from fastapi.testclient import TestClient


class VersionPlatform:
    async def list_space_versions(self, space: str):
        if space != "space":
            raise KeyError("space not found")
        return [{"id": "snapshot", "space_id": space, "note": "baseline"}]

    async def create_space_version(self, space: str, note: str):
        if space != "space":
            raise KeyError("space not found")
        if note == "busy":
            raise ValueError("space has active ingestion jobs")
        return {"id": "snapshot", "space_id": space, "note": note}

    async def set_space_version_note(self, space: str, version_id: str, note: str):
        if space != "space" or version_id != "snapshot":
            raise KeyError("space version not found")
        return {"id": version_id, "space_id": space, "note": note}

    async def activate_space_version(self, space: str, version_id: str):
        if space != "space" or version_id != "snapshot":
            raise KeyError("space version not found")
        raise ValueError("space version contains an incomplete child index")


def test_version_routes_validate_note_and_map_switch_conflicts() -> None:
    app = FastAPI()
    app.state.container = SimpleNamespace(knowledge=VersionPlatform())
    app.include_router(router)
    with TestClient(app) as client:
        base = "/api/v1/knowledge/spaces/space/versions"
        assert client.get(base).json()[0]["id"] == "snapshot"
        created = client.post(base, json={"note": "baseline"})
        assert created.status_code == 201
        assert created.json()["note"] == "baseline"
        saved = client.put(f"{base}/snapshot/note", json={"note": "v1 notes"})
        assert saved.status_code == 200
        assert saved.json()["note"] == "v1 notes"
        assert client.put(f"{base}/snapshot/note", json={"note": "x" * 501}).status_code == 422
        assert client.put(f"{base}/snapshot/note", json={"note": "", "extra": True}).status_code == 422
        assert client.put(
            "/api/v1/knowledge/spaces/other/versions/snapshot/note", json={"note": "x"}
        ).status_code == 404
        assert client.post(base, json={"note": "busy"}).status_code == 409
        refused = client.post(f"{base}/snapshot/activate")
        assert refused.status_code == 409
        assert "incomplete child index" in refused.json()["detail"]
        assert client.post(
            "/api/v1/knowledge/spaces/space/versions/other/activate"
        ).status_code == 404
