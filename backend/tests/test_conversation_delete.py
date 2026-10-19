"""Cascade deletion uses disposable data only; no production DB or provider API."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from atlasflow.conversations.api import events
from atlasflow.conversations.contracts import (
    ConfirmTurn,
    ConversationConflict,
    ConversationNotFoundError,
    CreateConversation,
    DeleteConversation,
    SendMessage,
)
from atlasflow.conversations.service import ConversationService
from atlasflow.conversations.storage import SQLiteConversationStore
from atlasflow.execution.contracts import PersistenceUnavailable, ResumeRunRequest
from atlasflow.main import create_app
from atlasflow.schemas import ApprovalAction, ApprovalRequest, RunRecord, RunStatus
from atlasflow.service import InvalidRunStateError, RunNotFoundError
from fastapi.testclient import TestClient

from backend.tests.fakes import GatewayScenario, make_test_container, make_test_settings
from backend.tests.test_conversations import ConversationModel, confirm, send, wait_turn
from backend.tests.test_execution_recovery import harness


def deletion(detail):
    return DeleteConversation(
        confirmation_conversation_id=detail.conversation.id,
        expected_version=detail.conversation.version,
    )


async def owned_run(service, conversation_id, *, content="做一份研究报告", request_id="research-1"):
    detail, _ = await send(service, conversation_id, content, request_id)
    detail = await confirm(service, detail)
    return detail.turns[-1].run_id


@pytest.mark.asyncio
async def test_delete_empty_conversation_and_second_delete_is_not_found():
    model = ConversationModel(())
    container = make_test_container(model=model)
    service = container.conversations
    conversation = await service.create(CreateConversation())
    detail = await service.detail(conversation.id)
    result = await service.delete(conversation.id, deletion(detail))
    assert result.deleted_run_count == 0 and result.conversation_id == conversation.id
    assert (await service.store.list())[1] == 0
    with pytest.raises(ConversationNotFoundError):
        await service.delete(conversation.id, deletion(detail))
    assert model.call_counts == {}


@pytest.mark.asyncio
async def test_delete_all_research_and_revision_runs_but_keep_unrelated_runs():
    model = ConversationModel(("research", "revise", "research"))
    container = make_test_container(model=model)
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    first = await owned_run(service, conversation.id)
    await wait_turn(service, conversation.id, {"completed"})
    second = await owned_run(
        service, conversation.id, content="保留内容，重新组织报告", request_id="revision"
    )
    detail = await wait_turn(service, conversation.id, {"completed"})
    other = await service.create(CreateConversation(auto_approve=True))
    other_run = await owned_run(service, other.id, request_id="other")
    await wait_turn(service, other.id, {"completed"})
    independent = await container.store.create(
        "独立运行，不属于该会话", source_run_id=first, auto_approve=True
    )
    before_calls = model.call_counts.copy()
    result = await service.delete(conversation.id, deletion(detail))
    assert result.deleted_run_count == 2 and model.call_counts == before_calls
    for run_id in (first, second):
        with pytest.raises(RunNotFoundError):
            await container.store.get(run_id)
    assert (await container.store.get(independent.id)).source_run_id == first
    assert (await container.store.get(other_run)).conversation_id == other.id
    assert (await service.detail(other.id)).conversation.id == other.id
    async with container.store.transaction() as connection:
        for table in ("conversation_turns", "conversation_messages"):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE conversation_id=?", (conversation.id,)
                ).fetchone()[0]
                == 0
            )
        for table, column in (
            ("runs", "id"),
            ("run_projections", "run_id"),
            ("run_events", "run_id"),
        ):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} IN (?,?)", (first, second)
                ).fetchone()[0]
                == 0
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_approve", [False, True])
async def test_delete_stops_waiting_or_running_run_and_does_not_resurrect(auto_approve):
    model = ConversationModel(scenario=GatewayScenario(research_delay_seconds=0.5))
    container = make_test_container(model=model)
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=auto_approve))
    run_id = await owned_run(service, conversation.id)
    if not auto_approve:
        await wait_turn(service, conversation.id, {"waiting_approval"})
    detail = await service.detail(conversation.id)
    task = container.run_service._tasks.get(run_id)
    await service.delete(conversation.id, deletion(detail))
    assert task is None or task.done()
    await asyncio.sleep(0)
    with pytest.raises(RunNotFoundError):
        await container.store.get(run_id)
    with pytest.raises(ConversationNotFoundError):
        await service.detail(conversation.id)


@pytest.mark.asyncio
async def test_delete_cancels_inflight_conversation_model_without_creating_a_run():
    started, stopped = asyncio.Event(), asyncio.Event()

    class SlowModel(ConversationModel):
        async def route_conversation(self, context):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    container = make_test_container(model=SlowModel())
    service = container.conversations
    conversation = await service.create(CreateConversation())
    await service.send(
        conversation.id, SendMessage(content="分析一个问题", request_id="slow", expected_version=0)
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    detail = await service.detail(conversation.id)
    result = await service.delete(conversation.id, deletion(detail))
    assert stopped.is_set() and result.deleted_run_count == 0 and not service._tasks
    assert (await container.store.list())[1] == 0


@pytest.mark.asyncio
async def test_wrong_confirmation_or_stale_version_cannot_start_deletion():
    container = make_test_container(model=ConversationModel(("clarify",)))
    service = container.conversations
    conversation = await service.create(CreateConversation())
    detail, _ = await send(service, conversation.id, "补充一个研究需求")
    with pytest.raises(ConversationConflict, match="不一致"):
        await service.delete(
            conversation.id,
            DeleteConversation(
                confirmation_conversation_id="other", expected_version=detail.conversation.version
            ),
        )
    with pytest.raises(ConversationConflict, match="已更新"):
        await service.delete(
            conversation.id,
            DeleteConversation(confirmation_conversation_id=conversation.id, expected_version=0),
        )
    preserved = await service.detail(conversation.id)
    assert not preserved.conversation.deleting and len(preserved.messages) == 2


@pytest.mark.asyncio
async def test_trace_only_version_change_does_not_invalidate_confirmation():
    container = make_test_container(model=ConversationModel(("clarify",)))
    service = container.conversations
    conversation = await service.create(CreateConversation())
    detail, _ = await send(service, conversation.id, "先澄清需求")
    payload = deletion(detail)
    await service.store.save_trace(
        conversation.id, detail.turns[-1].id, {"segment_id": "trace-only"}
    )
    assert (await service.detail(conversation.id)).conversation.version > payload.expected_version
    assert (await service.delete(conversation.id, payload)).deleted_run_count == 0


@pytest.mark.asyncio
async def test_owned_orphan_run_is_deleted_and_already_deleted_link_is_harmless():
    container = make_test_container(model=ConversationModel())
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    existing = await owned_run(service, conversation.id)
    detail = await wait_turn(service, conversation.id, {"completed"})
    await container.run_service.delete(existing, existing)
    orphan = RunRecord(query="与会话归属一致但未链接的准备记录", conversation_id=conversation.id)
    async with container.store.transaction() as connection:
        container.store.insert_prepared(connection, orphan)
    result = await service.delete(conversation.id, deletion(detail))
    assert result.deleted_run_count == 1
    with pytest.raises(RunNotFoundError):
        await container.store.get(orphan.id)


@pytest.mark.asyncio
async def test_corrupt_foreign_run_link_blocks_every_deletion():
    container = make_test_container(model=ConversationModel())
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    run_id = await owned_run(service, conversation.id)
    detail = await wait_turn(service, conversation.id, {"completed"})
    other = await container.store.create("其他独立Run", auto_approve=True)
    async with container.store.transaction() as connection:
        connection.execute(
            "UPDATE conversation_turns SET run_id=? WHERE conversation_id=?",
            (other.id, conversation.id),
        )
    with pytest.raises(ConversationConflict, match="归属"):
        await service.delete(conversation.id, deletion(detail))
    assert not (await service.detail(conversation.id)).conversation.deleting
    assert (await container.store.get(run_id)).id == run_id
    assert (await container.store.get(other.id)).id == other.id


@pytest.mark.asyncio
async def test_sql_error_rolls_back_entire_physical_cascade_and_can_retry():
    container = make_test_container(model=ConversationModel())
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    run_id = await owned_run(service, conversation.id)
    detail = await wait_turn(service, conversation.id, {"completed"})
    payload = deletion(detail)
    async with container.store.transaction() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_conversation_delete BEFORE DELETE ON conversations BEGIN SELECT RAISE(ABORT,'fixture rollback'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture rollback"):
        await service.delete(conversation.id, payload)
    preserved = await service.detail(conversation.id)
    assert preserved.conversation.deleting and preserved.messages
    assert (await container.store.get(run_id)).report
    with pytest.raises(ConversationConflict, match="正在删除"):
        await service.send(
            conversation.id,
            SendMessage(
                content="不能产生新请求",
                request_id="blocked",
                expected_version=preserved.conversation.version,
            ),
        )
    async with container.store.transaction() as connection:
        connection.execute("DROP TRIGGER fail_conversation_delete")
    # Same confirmation retries the already frozen deletion intent.
    assert (await service.delete(conversation.id, payload)).deleted_run_count == 1


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_records_blocks_another_service_and_is_retryable():
    container = make_test_container(model=ConversationModel())
    service = container.conversations
    conversation = await service.create(CreateConversation())
    detail, _ = await send(service, conversation.id, "创建执行建议")
    payload = deletion(detail)
    original = container.run_service.conversation_deletion

    @asynccontextmanager
    async def fail_cleanup(*args, **kwargs):
        raise PersistenceUnavailable("fixture: execution storage unavailable")
        yield

    container.run_service.conversation_deletion = fail_cleanup
    with pytest.raises(PersistenceUnavailable):
        await service.delete(conversation.id, payload)
    other_service = ConversationService(service.store, container.run_service, service.model)
    with pytest.raises(ConversationConflict, match="正在删除"):
        await other_service.confirm(
            conversation.id,
            detail.turns[-1].id,
            ConfirmTurn(
                expected_version=detail.conversation.version,
                confirmation_turn_id=detail.turns[-1].id,
            ),
        )
    assert (await service.detail(conversation.id)).messages
    container.run_service.conversation_deletion = original
    await service.delete(conversation.id, payload)


@pytest.mark.asyncio
async def test_pending_deletion_blocks_direct_approval_and_rerun_but_allows_cancellation():
    model = ConversationModel()
    container = make_test_container(model=model)
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=False))
    run_id = await owned_run(service, conversation.id)
    await wait_turn(service, conversation.id, {"waiting_approval"})
    detail = await service.detail(conversation.id)
    await service.store.begin_delete(conversation.id, detail.conversation.version)
    calls_before = model.call_counts.copy()
    with pytest.raises(InvalidRunStateError, match="正在删除"):
        await container.run_service.resolve_approval(
            run_id, ApprovalRequest(action=ApprovalAction.APPROVE)
        )
    with pytest.raises(InvalidRunStateError, match="正在删除"):
        await container.run_service.rerun(run_id)
    assert model.call_counts == calls_before and (await container.store.list())[1] == 1
    assert (await container.run_service.cancel(run_id)).status is RunStatus.CANCELLED
    await service.delete(conversation.id, deletion(detail))


@pytest.mark.asyncio
async def test_pending_deletion_blocks_launch_of_prepared_owned_run():
    container = make_test_container(model=ConversationModel(()))
    service = container.conversations
    conversation = await service.create(CreateConversation())
    run = RunRecord(query="尚未启动的会话Run", conversation_id=conversation.id)
    async with container.store.transaction() as connection:
        container.store.insert_prepared(connection, run)
    detail = await service.detail(conversation.id)
    await service.store.begin_delete(conversation.id, detail.conversation.version)
    with pytest.raises(InvalidRunStateError, match="正在删除"):
        await container.run_service.launch_prepared(run.id)
    assert not container.run_service._tasks
    await service.delete(conversation.id, deletion(detail))


@pytest.mark.asyncio
async def test_pending_deletion_blocks_direct_durable_resume(tmp_path):
    model = ConversationModel(())
    runtime, store, _, runs, _ = await harness(tmp_path, gateway=model)
    service = ConversationService(SQLiteConversationStore(store), runs, model)
    try:
        conversation = await service.create(CreateConversation())
        run = RunRecord(
            query="中断后的会话Run", conversation_id=conversation.id, status=RunStatus.INTERRUPTED
        )
        async with store.transaction() as connection:
            store.insert_prepared(connection, run)
        detail = await service.detail(conversation.id)
        await service.store.begin_delete(conversation.id, detail.conversation.version)
        with pytest.raises(InvalidRunStateError, match="正在删除"):
            await runs.resume(
                run.id,
                ResumeRunRequest(request_id="blocked-resume", expected_checkpoint_id="fixture"),
            )
        assert not runs._tasks and model.call_counts == {}
        assert await runtime.repository.read(run.id) is None
        await service.delete(conversation.id, deletion(detail))
    finally:
        await service.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.INTERRUPTED])
async def test_durable_deletion_of_owned_reservation_before_execution_binding(tmp_path, status):
    model = ConversationModel(())
    runtime, store, _, runs, _ = await harness(tmp_path, gateway=model)
    service = ConversationService(SQLiteConversationStore(store), runs, model)
    try:
        conversation = await service.create(CreateConversation())
        run = RunRecord(query="写入后尚未绑定执行", conversation_id=conversation.id, status=status)
        async with store.transaction() as connection:
            store.insert_prepared(connection, run)
        result = await service.delete(
            conversation.id, deletion(await service.detail(conversation.id))
        )
        assert result.deleted_run_count == 1 and model.call_counts == {}
        assert await runtime.repository.read(run.id) is None
        with pytest.raises(RunNotFoundError):
            await store.get(run.id)
    finally:
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_durable_deletion_stops_a_run_waiting_for_approval(tmp_path):
    model = ConversationModel()
    runtime, store, _, runs, _ = await harness(tmp_path, gateway=model)
    service = ConversationService(SQLiteConversationStore(store), runs, model)
    try:
        conversation = await service.create(CreateConversation(auto_approve=False))
        run_id = await owned_run(service, conversation.id)
        detail = await wait_turn(service, conversation.id, {"waiting_approval"})
        result = await service.delete(conversation.id, deletion(detail))
        assert result.deleted_run_count == 1
        assert (await runtime.repository.read(run_id)).control["deleted"]
        await runtime.flush_run(run_id)
        with pytest.raises(RunNotFoundError):
            await store.get(run_id)
    finally:
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_durable_deletion_fences_outbox_and_cannot_restore_runs_on_startup(tmp_path):
    model = ConversationModel()
    runtime, store, _, runs, _ = await harness(tmp_path, gateway=model)
    service = ConversationService(SQLiteConversationStore(store), runs, model)
    try:
        conversation = await service.create(CreateConversation(auto_approve=True))
        detail, _ = await send(service, conversation.id, "持久化执行测试")
        await confirm(service, detail)
        detail = await wait_turn(service, conversation.id, {"completed"})
        run_id = detail.turns[-1].run_id
        result = await service.delete(conversation.id, deletion(detail))
        assert (
            result.deleted_run_count == 1
            and (await runtime.repository.read(run_id)).control["deleted"]
        )
        await runtime.flush_run(run_id)
        await runs.reconcile_startup()
        with pytest.raises(RunNotFoundError):
            await store.get(run_id)
    finally:
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_sse_reports_deletion_and_closes_instead_of_reconnecting_forever():
    container = make_test_container(model=ConversationModel(()))
    service = container.conversations
    conversation = await service.create(CreateConversation())

    async def connected():
        return False

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(container=container)),
        headers={},
        is_disconnected=connected,
    )
    response = await events(conversation.id, request, last_event_id=0)
    iterator = response.body_iterator
    assert "retry:" in await anext(iterator)
    assert "conversation_snapshot" in await anext(iterator)
    await service.delete(conversation.id, deletion(await service.detail(conversation.id)))
    assert "conversation_deleted" in await anext(iterator)
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)


def test_delete_api_confirmation_validation_and_missing_conversation():
    container = make_test_container(model=ConversationModel(()))
    with TestClient(create_app(settings=make_test_settings(), container=container)) as client:
        created = client.post("/api/v1/conversations", json={"title": "待删除"}).json()
        path = f"/api/v1/conversations/{created['id']}"
        assert client.delete(path).status_code == 422
        assert (
            client.request(
                "DELETE",
                path,
                json={"confirmation_conversation_id": "wrong", "expected_version": 0},
            ).status_code
            == 409
        )
        assert client.get(path).status_code == 200
        response = client.request(
            "DELETE",
            path,
            json={"confirmation_conversation_id": created["id"], "expected_version": 0},
        )
        assert response.status_code == 200 and response.json()["deleted_run_count"] == 0
        assert client.get(path).status_code == 404
        assert client.get("/api/v1/conversations").json()["total"] == 0


@pytest.mark.parametrize("command", ["approval", "resume", "rerun"])
def test_pending_deletion_run_commands_return_conflict_instead_of_server_error(command):
    container = make_test_container(model=ConversationModel(()))
    method = "resolve_approval" if command == "approval" else command
    setattr(
        container.run_service, method, AsyncMock(side_effect=InvalidRunStateError("会话正在删除"))
    )
    payload = {
        "approval": {"action": "approve"},
        "resume": {"request_id": "blocked", "expected_checkpoint_id": "fixture"},
        "rerun": {},
    }[command]
    with TestClient(create_app(settings=make_test_settings(), container=container)) as client:
        # Rerun validates the source before dispatch, so supply a disposable Run.
        run = client.portal.call(container.store.create, "fixture Run")
        response = client.post(f"/api/v1/runs/{run.id}/{command}", json=payload)
        assert response.status_code == 409 and "正在删除" in response.json()["detail"]


def test_delete_api_cleanup_unavailable_preserves_conversation_and_returns_503():
    container = make_test_container(model=ConversationModel(()))
    with TestClient(create_app(settings=make_test_settings(), container=container)) as client:
        conversation = client.post("/api/v1/conversations", json={}).json()

        @asynccontextmanager
        async def unavailable(*args, **kwargs):
            raise PersistenceUnavailable("fixture: cleanup unavailable")
            yield

        container.run_service.conversation_deletion = unavailable
        path = f"/api/v1/conversations/{conversation['id']}"
        response = client.request(
            "DELETE",
            path,
            json={"confirmation_conversation_id": conversation["id"], "expected_version": 0},
        )
        assert response.status_code == 503
        preserved = client.get(path)
        assert preserved.status_code == 200 and preserved.json()["conversation"]["deleting"]
