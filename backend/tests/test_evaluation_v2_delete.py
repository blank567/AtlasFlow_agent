"""Deletion uses isolated SQLite records, never production data or model calls."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from atlasflow.evaluation.common.api import create_v2_router
from atlasflow.evaluation.common.service import V2EvaluationService
from atlasflow.evaluation.common.store import V2EvaluationStore


def saved(kind="rag", identifier="record", status="completed"):
    return {"experiment_id": identifier, "kind": kind, "status": status,
            "created_at": "2026-10-16T00:00:00Z", "results": [{"input_id": "evidence"}]}


def application(path):
    runtime = V2EvaluationService(V2EvaluationStore(path), datasets=[])

    @asynccontextmanager
    async def lifespan(_):
        await runtime.initialize()
        yield
        await runtime.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_v2_router("rag", runtime))
    app.include_router(create_v2_router("agent", runtime))
    return app, runtime


@pytest.mark.parametrize("kind", ["rag", "agent"])
def test_delete_hides_every_record_endpoint_and_preserves_evidence(tmp_path, kind):
    app, runtime = application(tmp_path / "delete.sqlite3")
    root = f"/api/v1/evaluations/{kind}"
    with TestClient(app) as client:
        original = saved(kind)
        client.portal.call(runtime.store.save, original)
        client.portal.call(runtime.store.save, saved(kind, "other"))
        evidence = {"input_id": "evidence", "kind": kind, "created_at": original["created_at"],
                    "experiment_id": "record", "artifact": {"content": "shared retained evidence"}}
        client.portal.call(runtime.store.register_input, evidence)
        wrong_kind = "agent" if kind == "rag" else "rag"
        assert client.delete(f"/api/v1/evaluations/{wrong_kind}/experiments/record").status_code == 404
        assert client.get(f"{root}/experiments/record").status_code == 200
        response = client.delete(f"{root}/experiments/record")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "deleted"
        assert response.json()["artifacts_retained"] is True
        assert client.delete(f"{root}/experiments/record").json() == response.json()
        assert client.delete(f"{root}/experiments/missing").status_code == 404
        assert client.delete(f"/api/v1/evaluations/{wrong_kind}/experiments/record").status_code == 404
        for suffix in ("", "/export", "/export?format=csv", "/comparison", "/artifacts/evidence"):
            assert client.get(f"{root}/experiments/record{suffix}").status_code == 404
        assert client.post(f"{root}/experiments/record/compare", json={}).status_code == 404
        assert client.post(f"{root}/experiments/record/cancel").status_code == 404
        assert client.post(f"{root}/compare", json={"baseline_id": "record", "candidate_id": "other"}).status_code == 404
        assert [row["experiment_id"] for row in client.get(f"{root}/experiments").json()] == ["other"]
        assert client.portal.call(runtime.store.list, kind, 1)[0]["experiment_id"] == "other"
        assert client.portal.call(runtime.store.get_input, "evidence") == evidence
        with pytest.raises(ValueError, match="deleted"):
            client.portal.call(runtime.store.save, original)
    # Tombstones survive process restart, and retained evidence remains immutable.
    app, runtime = application(tmp_path / "delete.sqlite3")
    with TestClient(app) as client:
        assert client.get(f"{root}/experiments/record").status_code == 404
        assert client.delete(f"{root}/experiments/record").json() == response.json()
        assert client.portal.call(runtime.store.get_input, "evidence") == evidence


def test_experiment_history_returns_summary_without_result_payload(tmp_path):
    app, runtime = application(tmp_path / "history.sqlite3")
    with TestClient(app) as client:
        record = {**saved(), "protocol": "rag-retrieval-v2", "plan": {"mode": "live", "case_ids": ["R1"]},
                  "context": {"large": "not for the history list"}, "summary": {"planned": 1},
                  "budget_ledger": {"http_attempts": 1}, "sync_status": "not_requested"}
        client.portal.call(runtime.store.save, record)
        response = client.get("/api/v1/evaluations/rag/experiments")
        assert response.status_code == 200
        history = response.json()[0]
        assert history["experiment_id"] == record["experiment_id"]
        assert history["plan"] == record["plan"] and history["summary"] == record["summary"]
        assert "results" not in history and "context" not in history


@pytest.mark.parametrize("kind", ["rag", "agent"])
@pytest.mark.parametrize("status", ["planned", "running"])
def test_active_experiments_cannot_be_deleted(tmp_path, kind, status):
    app, runtime = application(tmp_path / "active.sqlite3")
    with TestClient(app) as client:
        client.portal.call(runtime.store.save, saved(kind, status=status))
        root = f"/api/v1/evaluations/{kind}/experiments/record"
        assert client.delete(root).status_code == 409
        assert client.get(root).json()["status"] == status


async def test_terminal_status_during_background_finalization_cannot_be_deleted(tmp_path):
    app, runtime = application(tmp_path / "finalizing.sqlite3")
    await runtime.initialize()
    await runtime.store.save(saved())
    task = asyncio.create_task(asyncio.Event().wait())
    runtime._tasks["record"] = task
    try:
        with pytest.raises(ValueError, match="finalizing"):
            await runtime.delete("record", "rag")
        assert (await runtime.store.get("record"))["status"] == "completed"
    finally:
        await runtime.close()
