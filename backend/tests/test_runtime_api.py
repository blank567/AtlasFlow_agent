from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from atlasflow.agents.contracts import RunPolicy
from atlasflow.config import PROJECT_ROOT
from atlasflow.main import create_app
from atlasflow.observability import ProviderCallMetric, ProviderUsage, TraceSegment
from atlasflow.schemas import RunEvent, RunEventType, RunStatus, utc_now
from atlasflow.service import RunNotFoundError
from atlasflow.storage import SQLiteRunStore
from fastapi.testclient import TestClient

from backend.tests.fakes import GatewayScenario, make_test_container, make_test_settings
from backend.tests.test_api import _wait_for_status


def _database_path() -> Path:
    directory = PROJECT_ROOT / "data" / "test"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"runtime-{uuid4()}.sqlite3"


def _remove_database(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        path.with_name(f"{path.name}{suffix}").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_sqlite_store_persists_events_and_recovers_interrupted_runs() -> None:
    database = _database_path()
    first = SQLiteRunStore(database)
    try:
        run = await first.create(
            "验证 SQLite 重启恢复",
            policy=RunPolicy(initial_task_count=2),
        )
        event = RunEvent(
            run_id=run.id,
            event_type=RunEventType.RUN_STARTED,
            message="started",
        )
        await first.transition(run.id, RunStatus.RUNNING, event=event)
        await first.append_event(run.id, event)
        before_restart = await first.get(run.id)
        assert [item.sequence for item in before_restart.events] == [1]

        pending = await first.create("重启恢复 pending")
        waiting = await first.create("重启恢复 waiting approval")
        await first.transition(waiting.id, RunStatus.RUNNING)
        await first.transition(waiting.id, RunStatus.WAITING_APPROVAL)
    finally:
        await first.close()

    second = SQLiteRunStore(database)
    try:
        recovered = await second.get(run.id)
        assert recovered.status is RunStatus.FAILED
        assert recovered.error and "restarted" in recovered.error
        assert [item.sequence for item in recovered.events] == [1, 2]
        assert recovered.events[-1].data["reason"] == "backend_restart"
        for interrupted_id in (pending.id, waiting.id):
            interrupted = await second.get(interrupted_id)
            assert interrupted.status is RunStatus.FAILED
            assert interrupted.events[-1].data["reason"] == "backend_restart"
    finally:
        await second.close()
        _remove_database(database)


def test_run_listing_rerun_analytics_and_safe_settings_contracts() -> None:
    app = create_app(settings=make_test_settings(), container=make_test_container())
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/runs",
            json={"query": "第一份货币研究", "auto_approve": True},
        )
        assert first.status_code == 202
        first_id = first.json()["id"]
        first_final = _wait_for_status(client, first_id, {"completed", "failed"})
        assert first_final["status"] == "completed"
        assert len(first_final["trace_segments"]) == 1
        assert first_final["trace_segments"][0]["status"] == "completed"
        assert first_final["trace_segments"][0]["trace_status"] == "disabled"
        assert first_final["trace_segments"][0]["atlasflow_run_id"] == first_id
        trace_events = [
            event
            for event in first_final["events"]
            if event["event_type"] == "trace_segment_updated"
        ]
        assert [event["status"] for event in trace_events] == [
            "running",
            "completed",
        ]

        second = client.post(
            "/api/v1/runs",
            json={"query": "第二份黄山研究", "auto_approve": True},
        )
        assert second.status_code == 202
        second_id = second.json()["id"]
        assert _wait_for_status(client, second_id, {"completed"})["status"] == "completed"

        listing = client.get(
            "/api/v1/runs",
            params={"query": "货币", "status": "completed", "page_size": 1},
        )
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["pages"] == 1
        assert listing.json()["items"][0]["id"] == first_id

        listing_by_run_id = client.get(
            "/api/v1/runs",
            params={"query": first_id[:12]},
        )
        assert listing_by_run_id.status_code == 200
        assert listing_by_run_id.json()["total"] == 1
        assert listing_by_run_id.json()["items"][0]["id"] == first_id

        rerun = client.post(f"/api/v1/runs/{first_id}/rerun", json={})
        assert rerun.status_code == 202
        assert rerun.json()["source_run_id"] == first_id
        assert rerun.json()["query"] == "第一份货币研究"
        rerun_id = rerun.json()["id"]
        assert _wait_for_status(client, rerun_id, {"completed"})["status"] == "completed"

        analytics = client.get("/api/v1/analytics", params={"range": "all"})
        assert analytics.status_code == 200
        analytics_payload = analytics.json()
        assert analytics_payload["summary"]["total_runs"] == 3
        assert analytics_payload["summary"]["success_rate"] == 100.0
        assert analytics_payload["summary"]["task_success_rate"] == 100.0
        assert analytics_payload["summary"]["avg_quality_score"] == 90.0
        assert analytics_payload["summary"]["successful_tasks"] == 9
        assert analytics_payload["summary"]["failed_tasks"] == 0
        assert analytics_payload["summary"]["total_tasks"] == 9
        assert analytics_payload["status_distribution"] == [
            {"status": "completed", "count": 3}
        ]

        settings = client.get("/api/v1/settings/status")
        assert settings.status_code == 200
        settings_payload = settings.json()
        assert settings_payload["database"]["path"] == ":memory:"
        assert settings_payload["database"]["persistent"] is False
        assert settings_payload["langsmith"]["state"] == "disabled"
        assert settings_payload["provider"]["configured"] is True
        assert settings_payload["provider"]["name"] == "openrouter"
        assert "api_key" not in json.dumps(settings_payload).lower()


def test_cancel_delete_confirmation_and_sse_resume() -> None:
    container = make_test_container(
        scenario=GatewayScenario(research_delay_seconds=1.0)
    )
    app = create_app(settings=make_test_settings(), container=container)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs",
            json={"query": "取消一个耗时研究", "auto_approve": True},
        )
        run_id = created.json()["id"]
        cancelled = client.post(f"/api/v1/runs/{run_id}/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["events"][-1]["event_type"] == "run_cancelled"

        wrong_confirmation = client.request(
            "DELETE",
            f"/api/v1/runs/{run_id}",
            json={"confirmation_run_id": "wrong-run"},
        )
        assert wrong_confirmation.status_code == 409
        deleted = client.request(
            "DELETE",
            f"/api/v1/runs/{run_id}",
            json={"confirmation_run_id": run_id},
        )
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 404

        completed = client.post(
            "/api/v1/runs",
            json={"query": "验证 SSE 断点续传", "auto_approve": True},
        )
        completed_id = completed.json()["id"]
        final = _wait_for_status(client, completed_id, {"completed"})
        assert len(final["events"]) > 2
        with client.stream(
            "GET",
            f"/api/v1/runs/{completed_id}/events",
            headers={"Last-Event-ID": "1"},
        ) as stream:
            body = "".join(stream.iter_text())

        event_sequences = [
            json.loads(line.removeprefix("data: "))["sequence"]
            for line in body.splitlines()
            if line.startswith("data: {") and '"event_type"' in line
        ]
        assert event_sequences
        assert min(event_sequences) == 2
        assert all(sequence > 1 for sequence in event_sequences)
        invalid_cursor = client.get(
            f"/api/v1/runs/{completed_id}/events",
            headers={"Last-Event-ID": "not-a-number"},
        )
        assert invalid_cursor.status_code == 400


@pytest.mark.asyncio
async def test_delete_cascades_projection_and_events() -> None:
    store = SQLiteRunStore(":memory:")
    run = await store.create("验证级联删除")
    await store.transition(
        run.id,
        RunStatus.CANCELLED,
        event=RunEvent(
            run_id=run.id,
            event_type=RunEventType.RUN_CANCELLED,
            message="cancelled",
        ),
    )
    await store.delete(run.id)

    with pytest.raises(RunNotFoundError):
        await store.get(run.id)
    with pytest.raises(RunNotFoundError):
        await store.events_after(run.id, 0)
    await store.close()


@pytest.mark.asyncio
async def test_active_run_cannot_be_deleted_before_cancellation() -> None:
    store = SQLiteRunStore(":memory:")
    run = await store.create("活跃任务不能直接删除")
    with pytest.raises(RuntimeError, match="cancel the run first"):
        await store.delete(run.id)
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_event_appends_keep_contiguous_sequences() -> None:
    store = SQLiteRunStore(":memory:")
    run = await store.create("并发事件序号")
    await asyncio.gather(
        *(
            store.append_event(
                run.id,
                RunEvent(
                    run_id=run.id,
                    event_type=RunEventType.NODE_SUCCEEDED,
                    message=f"event-{index}",
                ),
            )
            for index in range(20)
        )
    )
    persisted = await store.get(run.id)
    assert [event.sequence for event in persisted.events] == list(range(1, 21))
    await store.close()


@pytest.mark.asyncio
async def test_trace_segment_upsert_keeps_one_segment_and_real_provider_usage() -> None:
    store = SQLiteRunStore(":memory:")
    run = await store.create("持久化 Trace Segment")
    started_at = utc_now()
    running = TraceSegment(
        segment_id="segment-1",
        trace_id="trace-1",
        atlasflow_run_id=run.id,
        name="atlasflow.workflow.initial",
        kind="initial",
        status="running",
        trace_status="active",
        started_at=started_at,
    )
    completed = running.model_copy(
        update={
            "status": "completed",
            "trace_status": "completed",
            "ended_at": utc_now(),
            "provider_calls": [
                ProviderCallMetric(
                    provider="openrouter",
                    operation="chat",
                    endpoint="/chat/completions",
                    status="succeeded",
                    requested_model="test/model",
                    model="test/model",
                    request_id="request-1",
                    usage=ProviderUsage(
                        prompt_tokens=10,
                        completion_tokens=5,
                        total_tokens=15,
                        cost=0.001,
                    ),
                    duration_ms=12,
                    attempt_count=1,
                    http_status=200,
                )
            ],
        }
    )

    await store.upsert_trace_segment(run.id, running)
    await store.upsert_trace_segment(run.id, completed)
    persisted = await store.get(run.id)

    assert len(persisted.trace_segments) == 1
    assert persisted.trace_segments[0].started_at == started_at
    assert persisted.trace_segments[0].provider_calls[0].usage is not None
    assert persisted.trace_segments[0].provider_calls[0].usage.total_tokens == 15
    assert [
        event.status
        for event in persisted.events
        if event.event_type is RunEventType.TRACE_SEGMENT_UPDATED
    ] == ["running", "completed"]
    await store.close()
