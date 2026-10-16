"""Opt-in real saver/journal integration, always a disposable E-project test schema."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest
from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.workflow import ResearchWorkflow
from atlasflow.execution import create_postgres_execution
from atlasflow.execution.contracts import PersistenceUnavailable
from atlasflow.execution.postgres import PostgresExecutionRepository
from atlasflow.schemas import ApprovalAction, ApprovalRequest, RunStatus
from atlasflow.service import RunService
from atlasflow.storage import SQLiteRunStore
from sqlalchemy.schema import DropSchema

from backend.tests.fakes import FakeModelGateway
from backend.tests.test_execution_recovery import wait_status
from scripts.init_execution_store import initialize_execution_store


@pytest.mark.skipif(not os.getenv("ATLASFLOW_TEST_DATABASE_URL"), reason="isolated PostgreSQL opt-in")
async def test_real_postgres_checkpoint_restart_approval_and_outbox(tmp_path):
    url = os.environ["ATLASFLOW_TEST_DATABASE_URL"]
    schema = f"atlasflow_execution_test_{uuid4().hex}"
    runtime = create_postgres_execution(url, schema=schema, profile_hash="test-pg-v1")
    store = SQLiteRunStore(tmp_path / "run.sqlite3", recover_interrupted=False, defer_open=True)
    service = None
    try:
        await initialize_execution_store(url, schema=schema)
        await runtime.start()
        await store.open()
        gateway = FakeModelGateway()
        workflow = ResearchWorkflow(model=gateway, event_sink=store.append_event, execution=runtime,
                                    trace_segment_sink=store.upsert_trace_segment)
        service = RunService(store, workflow, execution=runtime)
        run = await service.create("真实PG审批恢复", auto_approve=False,
                                   policy=RunPolicy(knowledge_mode="off"))
        await wait_status(store, run.id, {RunStatus.WAITING_APPROVAL})
        await service.close()
        await runtime.close()
        await store.close()

        runtime = create_postgres_execution(url, schema=schema, profile_hash="test-pg-v1")
        store = SQLiteRunStore(tmp_path / "run.sqlite3", recover_interrupted=False, defer_open=True)
        await runtime.start()
        await store.open()
        workflow = ResearchWorkflow(model=gateway, event_sink=store.append_event, execution=runtime,
                                    trace_segment_sink=store.upsert_trace_segment)
        service = RunService(store, workflow, execution=runtime)
        await service.reconcile_startup()
        assert (await store.get(run.id)).status is RunStatus.WAITING_APPROVAL
        recovery = await service.inspect_recovery(run.id)
        await service.resolve_approval(run.id, ApprovalRequest(
            action=ApprovalAction.APPROVE, request_id="pg-approval",
            expected_checkpoint_id=recovery.checkpoint_id))
        final = await wait_status(store, run.id, RunStatus.terminal())
        task = service._tasks.get(run.id)
        if task is not None:
            await task
        assert final.status is RunStatus.COMPLETED
        assert gateway.call_counts["create_plan"] == 1
        bundle = await runtime.repository.read(run.id)
        assert all(item["delivered"] for item in bundle.outbox.values())
        assert len({event.event_id for event in final.events}) == len(final.events)
    finally:
        if service:
            await service.close()
        await runtime.close()
        await store.close()
        # Explicit schema is generated above, validated by execution_tables and
        # never the production schema/public/database. No user data is removed.
        async with runtime.repository.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await runtime.repository.engine.dispose()


@pytest.mark.skipif(not os.getenv("ATLASFLOW_TEST_DATABASE_URL"), reason="isolated PostgreSQL opt-in")
async def test_real_process_kill_restart_preserves_partial_wave(tmp_path):
    url = os.environ["ATLASFLOW_TEST_DATABASE_URL"]
    schema = f"atlasflow_execution_test_{uuid4().hex}"
    repository = PostgresExecutionRepository(url, schema=schema)
    first = second = None
    try:
        await initialize_execution_store(url, schema=schema)
        command = [sys.executable, "-m", "backend.tests.execution_crash_worker",
                   "--schema", schema, "--store", str(tmp_path / "crash.sqlite3")]
        first = await asyncio.create_subprocess_exec(*command, "--phase", "start",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        line = await asyncio.wait_for(first.stdout.readline(), 30)
        assert line, (await first.stderr.read()).decode(errors="replace")
        run_id = json.loads(line)["run_id"]
        for _ in range(500):
            bundle = await repository.read(run_id)
            completed = [item for item in bundle.invocations.values()
                         if item["kind"] == "node_result" and item["name"] == "researcher"
                         and item["status"] == "succeeded"]
            if completed:
                break
            await asyncio.sleep(0.02)
        assert len(completed) == 1
        # Actual process termination, not CancelledError or controlled shutdown.
        first.kill()
        await asyncio.wait_for(first.wait(), 10)
        second = await asyncio.create_subprocess_exec(*command, "--phase", "resume", "--run-id", run_id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, error = await asyncio.wait_for(second.communicate(), 90)
        assert second.returncode == 0, error.decode(errors="replace")
        result = json.loads(output.decode().splitlines()[-1])
        assert result["status"] == "completed" and result["run_id"] == run_id
        assert result["research_calls"] == 2 and result["planner_calls"] == 0
        assert result["tasks"] == 3
        assert len(set(result["event_ids"])) == len(result["event_ids"])
        assert result["sequences"] == list(range(1, len(result["sequences"]) + 1))
    finally:
        for process in (first, second):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
        async with repository.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await repository.close()


@pytest.mark.skipif(not os.getenv("ATLASFLOW_TEST_DATABASE_URL"), reason="isolated PostgreSQL opt-in")
async def test_pg_unready_verification_never_opens_sqlite(tmp_path):
    # A missing generated schema reliably exercises readiness failure without
    # stopping the user's PostgreSQL daemon or touching its production tables.
    schema = f"atlasflow_execution_test_{uuid4().hex}"
    runtime = create_postgres_execution(os.environ["ATLASFLOW_TEST_DATABASE_URL"], schema=schema)
    path = tmp_path / "unopened" / "runs.sqlite3"
    store = SQLiteRunStore(path, recover_interrupted=False, defer_open=True)
    try:
        with pytest.raises(PersistenceUnavailable):
            await runtime.start()
        assert not path.exists() and not path.parent.exists()
    finally:
        await runtime.close()
        await store.close()


@pytest.mark.skipif(not os.getenv("ATLASFLOW_TEST_DATABASE_URL"), reason="isolated PostgreSQL opt-in")
def test_create_app_production_factory_unready_leaves_run_sqlite_absent(tmp_path):
    from atlasflow.bootstrap import ProviderBundle, build_container
    from atlasflow.main import create_app
    from fastapi.testclient import TestClient

    from backend.tests.fakes import FakeEmbeddingProvider, FakeRerankProvider, make_test_settings

    url = os.environ["ATLASFLOW_TEST_DATABASE_URL"]
    path = tmp_path / "runs" / "production-factory.sqlite3"
    settings = make_test_settings().model_copy(update={
        "knowledge_backend": "postgres", "knowledge_database_url": url,
        "execution_database_url": url, "database_path": str(path),
        "knowledge_blob_root": str(tmp_path / "blobs"),
        "evaluation_storage_root": str(tmp_path / "evaluations"),
    })
    container = build_container(settings, providers=ProviderBundle(
        model=FakeModelGateway(), embedding=FakeEmbeddingProvider(), reranker=FakeRerankProvider()))
    original_runtime = container.execution
    schema = f"atlasflow_execution_test_{uuid4().hex}"
    container.execution = create_postgres_execution(url, schema=schema)
    container.run_service.execution = container.execution
    container.execution.bind_store(container.store)
    try:
        assert not path.exists() and not path.parent.exists()
        with pytest.raises(PersistenceUnavailable), TestClient(create_app(settings=settings, container=container)):
            pass
        assert not path.exists() and not path.parent.exists()
    finally:
        asyncio.run(original_runtime.close())
