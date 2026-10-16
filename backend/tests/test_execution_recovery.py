"""Deterministic recovery contracts; no network/model calls or production data."""
from __future__ import annotations

import asyncio

import pytest
from atlasflow.agents.contracts import ExecutionMetrics, RunPolicy
from atlasflow.agents.workflow import _CHECKPOINT_TYPES, ResearchWorkflow
from atlasflow.execution.context import NodeContext
from atlasflow.execution.contracts import (
    ExecutionConflict,
    PersistenceUnavailable,
    ResumeRunRequest,
    UnknownInvocation,
    decode_safe,
    encode_safe,
)
from atlasflow.execution.memory import MemoryExecutionRepository
from atlasflow.execution.runtime import ExecutionRuntime, ExecutionToolLimit
from atlasflow.schemas import ApprovalAction, ApprovalRequest, RunEvent, RunEventType, RunStatus
from atlasflow.service import RunService
from atlasflow.storage import SQLiteRunStore
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from backend.tests.fakes import FakeModelGateway, GatewayScenario


async def harness(tmp_path, *, repository=None, saver=None, gateway=None):
    repository = repository or MemoryExecutionRepository()
    saver = saver or MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=_CHECKPOINT_TYPES))
    runtime = ExecutionRuntime(repository, saver, profile_hash="test-graph-v1")
    await runtime.start()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3", recover_interrupted=False)
    gateway = gateway or FakeModelGateway()
    workflow = ResearchWorkflow(model=gateway, event_sink=store.append_event, execution=runtime,
                                trace_segment_sink=store.upsert_trace_segment)
    service = RunService(store, workflow, execution=runtime)
    return runtime, store, workflow, service, gateway


async def wait_status(store, run_id, statuses):
    for _ in range(3000):
        record = await store.get(run_id)
        if record.status in statuses:
            return record
        if record.status in RunStatus.terminal() or record.status is RunStatus.INTERRUPTED:
            raise AssertionError(f"Unexpected {record.status}: {record.error or record.recovery.reason}")
        await asyncio.sleep(0.01)
    raise AssertionError("Run did not reach the expected status")


async def test_durable_direct_path_and_atomic_outbox(tmp_path):
    runtime, store, _, service, _ = await harness(tmp_path)
    try:
        run = await service.execute_and_wait("验证完整持久化执行", policy=RunPolicy(knowledge_mode="off"))
        assert run.status is RunStatus.COMPLETED
        bundle = await runtime.repository.read(run.id)
        assert all(item["delivered"] for item in bundle.outbox.values())
        before = len(run.events)
        # Crash after SQLite commit but before PG delivery acknowledgment.
        first = next(iter(bundle.outbox))
        await runtime.repository.mutate(run.id, lambda b: b.outbox[first].update(delivered=False))
        await runtime.flush_run(run.id)
        assert len((await store.get(run.id)).events) == before
        assert any(i["kind"] == "node_result" and i["name"] == "researcher"
                   for i in bundle.invocations.values())
    finally:
        await service.close()
        await store.close()


async def test_approval_survives_restart_and_duplicate_command(tmp_path):
    runtime, store, workflow, service, gateway = await harness(tmp_path)
    run = await service.create("人工审批重启恢复", auto_approve=False,
                               policy=RunPolicy(knowledge_mode="off"))
    await wait_status(store, run.id, {RunStatus.WAITING_APPROVAL})
    await service.close()
    await store.close()
    _replacement, store2, _, service2, _ = await harness(
        tmp_path, repository=runtime.repository, saver=workflow.checkpointer, gateway=gateway)
    try:
        await service2.reconcile_startup()
        assert (await store2.get(run.id)).status is RunStatus.WAITING_APPROVAL
        info = await service2.inspect_recovery(run.id)
        request = ApprovalRequest(action=ApprovalAction.APPROVE, request_id="approved-once",
                                  expected_checkpoint_id=info.checkpoint_id)
        await asyncio.gather(service2.resolve_approval(run.id, request),
                             service2.resolve_approval(run.id, request))
        final = await wait_status(store2, run.id, RunStatus.terminal())
        assert final.status is RunStatus.COMPLETED
        assert gateway.call_counts["create_plan"] == 1
        assert gateway.call_counts["analyze_task"] == 3
        assert final.metrics.model_calls == 7
    finally:
        await service2.close()
        await store2.close()


async def test_partial_parallel_wave_resume_same_run(tmp_path):
    class InterruptedGateway(FakeModelGateway):
        stop = True

        async def analyze_task(self, query, task, dependencies=(), **kwargs):
            if self.stop and task.task_id != "p1-t1":
                await asyncio.sleep(20)
            return await super().analyze_task(query, task, dependencies, **kwargs)

    gateway = InterruptedGateway(GatewayScenario(research_delay_seconds=0.001))
    runtime, store, workflow, service, _ = await harness(tmp_path, gateway=gateway)
    run = await service.create("保存并行波次已完成结果", policy=RunPolicy(knowledge_mode="off"))
    for _ in range(300):
        record = await store.get(run.id)
        if any(e.event_type is RunEventType.TASK_COMPLETED for e in record.events):
            break
        await asyncio.sleep(0.01)
    await service.close()
    await store.close()
    gateway.stop = False
    _replacement, store2, _, service2, _ = await harness(
        tmp_path, repository=runtime.repository, saver=workflow.checkpointer, gateway=gateway)
    try:
        await service2.reconcile_startup()
        before = await store2.get(run.id)
        assert before.status is RunStatus.INTERRUPTED
        recovery = await service2.inspect_recovery(run.id)
        assert recovery.available
        request = ResumeRunRequest(request_id="resume-once", expected_checkpoint_id=recovery.checkpoint_id)
        await asyncio.gather(service2.resume(run.id, request), service2.resume(run.id, request))
        final = await wait_status(store2, run.id, RunStatus.terminal())
        assert final.status is RunStatus.COMPLETED
        assert gateway.research_attempts["p1-t1"] == 1
        assert len(final.research_results) == 3
        assert [e.sequence for e in final.events] == list(range(1, len(final.events) + 1))
        assert final.trace_segments[-1].name == "atlasflow.workflow.crash_resume"
    finally:
        await service2.close()
        await store2.close()


async def test_unknown_ack_budget_and_fencing(tmp_path):
    runtime, store, _, _service, _ = await harness(tmp_path)
    try:
        run = await store.create("测试调用预算")
        await runtime.bind_run(run.id, tool_limit=1)
        claim, _ = await runtime.claim(run.id, request_id="start", payload={"kind": "initial"})
        context = NodeContext(runtime, claim, "stable-task")
        lease = await runtime.begin_invocation(context, kind="tool", name="calculator", inputs={"x": 1},
                                                consumes_tool_budget=True)
        await runtime.dispatch(lease)
        await runtime.recover_owner(run.id)
        with pytest.raises(ExecutionConflict):
            await runtime.confirm(lease, {"success": True})
        with pytest.raises(UnknownInvocation):
            await runtime.claim(run.id, request_id="resume", payload={"kind": "resume"})
        replacement, _ = await runtime.claim(run.id, request_id="resume", payload={"kind": "resume"},
                                             acknowledged=[lease.invocation_id])
        with pytest.raises(ExecutionToolLimit):
            await runtime.begin_invocation(NodeContext(runtime, replacement, "stable-task"),
                kind="tool", name="calculator", inputs={"x": 1}, consumes_tool_budget=True)
        assert (await runtime.repository.read(run.id)).control["tool_reserved"] == 1
    finally:
        await store.close()


async def test_completed_node_hides_irrelevant_inner_unknown(tmp_path):
    runtime, store, _, _, _ = await harness(tmp_path)
    try:
        run = await store.create("完成节点无需重复确认")
        await runtime.bind_run(run.id, tool_limit=20)
        claim, _ = await runtime.claim(run.id, request_id="start", payload={})
        context = NodeContext(runtime, claim, "task-1")
        http = await runtime.begin_invocation(context, kind="http", name="chat", inputs={}, replayable=False)
        await runtime.dispatch(http)
        node = await runtime.begin_invocation(context, kind="node_result", name="researcher", inputs={})
        await runtime.confirm(node, {})
        bundle = await runtime.recover_owner(run.id)
        assert runtime._active_unknown(bundle) == []
    finally:
        await store.close()


async def test_delete_tombstone_discards_late_outbox(tmp_path):
    runtime, store, _, service, _ = await harness(tmp_path)
    try:
        run = await service.execute_and_wait("删除不复活运行", policy=RunPolicy(knowledge_mode="off"))
        bundle = await runtime.repository.read(run.id)
        for key in bundle.outbox:
            await runtime.repository.mutate(run.id, lambda b, key=key: b.outbox[key].update(delivered=False))
        await service.delete(run.id, run.id)
        await service.reconcile_startup()
        assert (await runtime.repository.read(run.id)).control["deleted"]
        await runtime.flush_run(run.id)
        from atlasflow.service import RunNotFoundError
        with pytest.raises(RunNotFoundError):
            await store.get(run.id)
    finally:
        await store.close()


async def test_storage_failure_is_not_a_business_retry(tmp_path):
    class FailingConfirmation(MemoryExecutionRepository):
        async def mutate(self, run_id, operation):
            before = await self.read(run_id)
            # Raise exactly while writing the Researcher safe output, after the
            # successful external/fake model call, not before request dispatch.
            def guarded(bundle):
                result = operation(bundle)
                for key, item in bundle.invocations.items():
                    old = before.invocations.get(key)
                    if item["kind"] == "node_result" and item["name"] == "researcher" and \
                            item["status"] == "succeeded" and old and old["status"] != "succeeded":
                        raise PersistenceUnavailable("test result commit interrupted")
                return result
            return await super().mutate(run_id, guarded)

    _runtime, store, _, service, gateway = await harness(tmp_path, repository=FailingConfirmation())
    try:
        run = await service.execute_and_wait("账本失败不追加调用", policy=RunPolicy(knowledge_mode="off"))
        assert run.status is RunStatus.INTERRUPTED
        assert all(attempt == 1 for attempt in gateway.research_attempts.values())
    finally:
        await store.close()


def test_metrics_and_safe_types_roundtrip_without_pickle():
    original = {"metrics": ExecutionMetrics(), "status": RunStatus.INTERRUPTED}
    restored = decode_safe(encode_safe(original))
    assert restored == original
    with pytest.raises(ExecutionConflict):
        decode_safe({"$type": "arbitrary.module.Evil", "data": {}})


async def test_lazy_sqlite_is_unchanged_before_open(tmp_path):
    path = tmp_path / "not-created" / "runs.sqlite3"
    store = SQLiteRunStore(path, recover_interrupted=False, defer_open=True)
    assert not path.exists() and not path.parent.exists()
    await store.close()
    assert not path.exists()


async def test_confirmed_tool_intent_and_result_replay_at_exhausted_budget(tmp_path):
    from atlasflow.agents.tool_runtime import ToolRuntime
    from atlasflow.execution.context import CURRENT_EXECUTION
    from atlasflow.execution.runtime import DurableToolBudget
    from atlasflow.tools import ToolRegistry
    from atlasflow.tools.calculator import CalculatorTool

    from backend.tests.test_tool_runtime import _call, _client, _response

    class CountingCalculator(CalculatorTool):
        executions = 0

        async def run(self, arguments, context):
            self.executions += 1
            return await super().run(arguments, context)

    runtime, store, _, _, _ = await harness(tmp_path)
    run = await store.create("工具确认后不重复执行")
    await runtime.bind_run(run.id, tool_limit=1)
    claim, _ = await runtime.claim(run.id, request_id="initial", payload={})
    registry = ToolRegistry()
    tool = CountingCalculator()
    registry.register(tool)
    requests = []

    def handler(request):
        requests.append(request)
        return _response(_call("calculator", {"expression": "1+1"}))

    tools = ToolRuntime(registry=registry, client=_client(handler), model="test/model",
                        event_sink=runtime.emit)
    tools.budget = DurableToolBudget(runtime)
    try:
        for _ in range(2):
            token = CURRENT_EXECUTION.set(NodeContext(runtime, claim, "fixed-task"))
            try:
                stage = await tools.gather(run_id=run.id, agent="researcher", prompt="1+1=?",
                    policy=RunPolicy(max_tool_calls_per_turn=1, max_tool_calls_per_run=1),
                    required_capabilities=["calculator"])
                assert stage.records[0].success
                assert "1+1 = 2" in stage.context
            finally:
                CURRENT_EXECUTION.reset(token)
        assert tool.executions == len(requests) == 1
        assert (await runtime.repository.read(run.id)).control["tool_reserved"] == 1
    finally:
        await store.close()


async def test_accepted_approval_before_dispatch_resumes_without_second_approval(tmp_path):
    runtime, store, workflow, service, gateway = await harness(tmp_path)
    run = await service.create("已批准命令崩溃恢复", auto_approve=False,
                               policy=RunPolicy(knowledge_mode="off"))
    await wait_status(store, run.id, {RunStatus.WAITING_APPROVAL})
    checkpoint_id = (await service.inspect_recovery(run.id)).checkpoint_id
    await runtime.claim(run.id, request_id="accepted-before-crash", checkpoint_id=checkpoint_id,
                        payload={"kind": "approval", "action": "approve", "edited_plan": None})
    await store.transition(run.id, RunStatus.RUNNING)
    await store.close()
    _replacement, store2, _, service2, _ = await harness(
        tmp_path, repository=runtime.repository, saver=workflow.checkpointer, gateway=gateway)
    try:
        await service2.reconcile_startup()
        assert (await store2.get(run.id)).status is RunStatus.INTERRUPTED
        info = await service2.inspect_recovery(run.id)
        assert info.available
        await service2.resume(run.id, ResumeRunRequest(request_id="continue-accepted-command",
                                                       expected_checkpoint_id=info.checkpoint_id))
        final = await wait_status(store2, run.id, RunStatus.terminal())
        assert final.status is RunStatus.COMPLETED
        assert gateway.call_counts["create_plan"] == 1
        assert final.trace_segments[-1].name == "atlasflow.workflow.crash_resume"
    finally:
        await service2.close()
        await store2.close()


async def test_shutdown_does_not_change_terminal_run(tmp_path):
    _runtime, store, _, service, _ = await harness(tmp_path)
    run = await service.execute_and_wait("终态退出不误标中断", policy=RunPolicy(knowledge_mode="off"))
    await service.close()
    assert (await store.get(run.id)).status is RunStatus.COMPLETED
    await store.close()


async def test_cancel_intent_discards_pending_success_projection(tmp_path):
    runtime, store, _, _, _ = await harness(tmp_path)
    try:
        run = await store.create("取消优先于未投递终态")
        await runtime.bind_run(run.id, tool_limit=20)
        event = RunEvent(run_id=run.id, event_type=RunEventType.RUN_COMPLETED, message="pending success")
        await runtime.repository.mutate(run.id, lambda bundle: bundle.outbox.update({event.event_id: {
            "event": event.model_dump(mode="json"), "patch": encode_safe({
                "status": RunStatus.COMPLETED, "report": "draft"}), "delivered": False, "order": 0}}))
        await runtime.cancel(run.id)
        await runtime.flush_run(run.id)
        assert (await store.get(run.id)).status is RunStatus.PENDING
        bundle = await runtime.repository.read(run.id)
        assert bundle.outbox[event.event_id]["discarded"]
    finally:
        await store.close()


async def test_run_binding_uses_snapshot_callback_except_off(tmp_path):
    runtime, store, workflow, _service, _ = await harness(tmp_path)
    calls = []
    snapshot = {"space_id": "immutable-test-space", "generation_id": "immutable-test-generation"}

    async def capture(policy):
        calls.append(policy.knowledge_mode)
        return snapshot

    service = RunService(store, workflow, execution=runtime, snapshot_factory=capture)
    try:
        first = await service.execute_and_wait("固定同Run知识快照")
        assert first.retrieval_snapshot == snapshot
        graph = await workflow.graph.aget_state(workflow._config(first.id))
        assert graph.values["retrieval_snapshot"] == snapshot
        await service.execute_and_wait("知识检索禁用不捕获", policy=RunPolicy(knowledge_mode="off"))
        assert calls == ["default"]
    finally:
        await store.close()


async def test_http_groups_preserve_retries_and_repair_request_identity(tmp_path):
    from atlasflow.execution.context import (
        CURRENT_EXECUTION,
        CURRENT_INVOCATION,
        after_http_attempt,
        before_http_attempt,
    )
    runtime, store, _, _, _ = await harness(tmp_path)
    run = await store.create("分组统计物理尝试和逻辑调用")
    await runtime.bind_run(run.id, tool_limit=20)
    claim, _ = await runtime.claim(run.id, request_id="initial", payload={})
    token = CURRENT_EXECUTION.set(NodeContext(runtime, claim, "http-group-node"))
    scope = CURRENT_INVOCATION.set("persisted-model-invocation-1")
    try:
        failed = await before_http_attempt(endpoint="/chat/completions", requested_model="test/model",
                                            attempt_number=1)
        await after_http_attempt(failed, outcome="failed", http_status=500)
        retry = await before_http_attempt(endpoint="/chat/completions", requested_model="test/model",
                                           attempt_number=2)
        await after_http_attempt(retry, outcome="succeeded", http_status=200,
                                 usage={"total_tokens": 10, "cost": 0.1})
        repair = await before_http_attempt(endpoint="/chat/completions", requested_model="test/model",
                                            attempt_number=1)
        await after_http_attempt(repair, outcome="succeeded", http_status=200,
                                 usage={"total_tokens": 20, "cost": 0.2})
        summary = runtime.provider_summary(await runtime.repository.read(run.id))
        assert summary["http_attempts"] == 3
        assert summary["provider_requests"] == summary["model_calls"] == 2
        assert summary["known_tokens"] == 30 and summary["known_cost"] == pytest.approx(0.3)
        assert summary["token_usage_coverage"] == summary["cost_usage_coverage"] == pytest.approx(2 / 3)
        # A resumed new model invocation must not collide with HTTP groups that
        # belonged to skipped/replayed tool decisions earlier in the same node.
        CURRENT_INVOCATION.reset(scope)
        scope = CURRENT_INVOCATION.set("persisted-model-invocation-2")
        new = await before_http_attempt(endpoint="/chat/completions", requested_model="test/model")
        await after_http_attempt(new, outcome="succeeded", http_status=200)
        assert runtime.provider_summary(await runtime.repository.read(run.id))["model_calls"] == 3
    finally:
        CURRENT_INVOCATION.reset(scope)
        CURRENT_EXECUTION.reset(token)
        await store.close()
