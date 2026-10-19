from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import uuid4

from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.workflow import ResearchWorkflow, WorkflowExecution
from atlasflow.execution.contracts import (
    ExecutionConflict,
    ExecutionControlError,
    PersistenceUnavailable,
    RecoveryInfo,
    ResumeRunRequest,
    UnknownCall,
)
from atlasflow.execution.runtime import ExecutionRuntime
from atlasflow.observability import TraceSegment
from atlasflow.schemas import (
    AnalyticsResponse,
    AnalyticsSummary,
    ApprovalRequest,
    DecisionCount,
    DurationTrendPoint,
    PlanVersionCount,
    RunEvent,
    RunEventType,
    RunRecord,
    RunStatus,
    StatusCount,
    utc_now,
)


class RunNotFoundError(KeyError):
    pass


class InvalidRunStateError(RuntimeError):
    pass


_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED,
                                 RunStatus.INTERRUPTED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_WARNINGS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.INTERRUPTED}
    ),
    RunStatus.INTERRUPTED: frozenset({RunStatus.RUNNING, RunStatus.WAITING_APPROVAL,
                                    RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.COMPLETED_WITH_WARNINGS: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def allowed_transitions(status: RunStatus) -> frozenset[RunStatus]:
    return _ALLOWED_TRANSITIONS[status]


class RunStore(Protocol):
    async def create(
        self,
        query: str,
        *,
        auto_approve: bool = True,
        policy: RunPolicy | None = None,
        source_run_id: str | None = None,
    ) -> RunRecord: ...

    async def get(self, run_id: str) -> RunRecord: ...

    async def append_event(self, run_id: str, event: RunEvent) -> None: ...

    async def events_after(self, run_id: str, after_sequence: int) -> list[RunEvent]: ...

    async def event_batch(
        self, run_id: str, after_sequence: int
    ) -> tuple[list[RunEvent], RunStatus]: ...

    async def update(self, run_id: str, **changes: object) -> RunRecord: ...

    async def transition(
        self,
        run_id: str,
        status: RunStatus,
        *,
        event: RunEvent | None = None,
        **changes: object,
    ) -> RunRecord: ...

    async def list(
        self,
        *,
        query: str | None = None,
        status: RunStatus | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[RunRecord], int]: ...

    async def records_since(self, since: datetime | None) -> list[RunRecord]: ...

    async def delete(self, run_id: str) -> None: ...

    async def ensure_conversation_writable(self, conversation_id: str) -> None: ...

    async def upsert_trace_segment(self, run_id: str, segment: TraceSegment) -> None: ...

    async def apply_outbox(self, payload: dict[str, Any], patch: dict[str, Any]) -> None: ...


class InMemoryRunStore:
    """Backwards-compatible lightweight store; runtime uses SQLiteRunStore."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        query: str,
        *,
        auto_approve: bool = True,
        policy: RunPolicy | None = None,
        source_run_id: str | None = None,
    ) -> RunRecord:
        record = RunRecord(
            query=query,
            auto_approve=auto_approve,
            policy=policy or RunPolicy(),
            source_run_id=source_run_id,
        )
        async with self._lock:
            self._runs[record.id] = record
        return record.model_copy(deep=True)

    async def get(self, run_id: str) -> RunRecord:
        async with self._lock:
            return self._get(run_id).model_copy(deep=True)

    async def append_event(self, run_id: str, event: RunEvent) -> None:
        if event.run_id != run_id:
            raise ValueError("event.run_id must match the target run")
        async with self._lock:
            record = self._get(run_id)
            if any(item.event_id == event.event_id for item in record.events):
                return
            sequenced = event.model_copy(update={"sequence": len(record.events) + 1})
            updated = self._validated_update(record, events=[*record.events, sequenced])
            self._runs[run_id] = updated

    async def apply_outbox(self, payload: dict[str, Any], patch: dict[str, Any]) -> None:
        event = RunEvent.model_validate(payload)
        async with self._lock:
            record = self._get(event.run_id)
            if any(item.event_id == event.event_id for item in record.events):
                return
            sequenced = event.model_copy(update={"sequence": len(record.events) + 1})
            self._runs[event.run_id] = self._validated_update(
                record, **patch, events=[*record.events, sequenced])

    async def event_batch(
        self, run_id: str, after_sequence: int
    ) -> tuple[list[RunEvent], RunStatus]:
        async with self._lock:
            record = self._get(run_id)
            return (
                [
                    event.model_copy(deep=True)
                    for event in record.events
                    if event.sequence > after_sequence
                ],
                record.status,
            )

    async def events_after(self, run_id: str, after_sequence: int) -> list[RunEvent]:
        events, _ = await self.event_batch(run_id, after_sequence)
        return events

    async def update(self, run_id: str, **changes: object) -> RunRecord:
        async with self._lock:
            record = self._get(run_id)
            updated = self._validated_update(record, **changes)
            self._runs[run_id] = updated
            return updated.model_copy(deep=True)

    async def transition(
        self,
        run_id: str,
        status: RunStatus,
        *,
        event: RunEvent | None = None,
        **changes: object,
    ) -> RunRecord:
        """Atomically persist artifacts, a status transition, and its terminal event."""

        if event is not None and event.run_id != run_id:
            raise ValueError("event.run_id must match the target run")
        async with self._lock:
            record = self._get(run_id)
            if status is not record.status and status not in _ALLOWED_TRANSITIONS[record.status]:
                raise InvalidRunStateError(
                    f"Cannot transition run {run_id} from {record.status.value} to {status.value}"
                )
            events = list(record.events)
            if event is not None:
                events.append(event.model_copy(update={"sequence": len(events) + 1}))
            updated = self._validated_update(record, status=status, events=events, **changes)
            self._runs[run_id] = updated
            return updated.model_copy(deep=True)

    async def upsert_trace_segment(self, run_id: str, segment: TraceSegment) -> None:
        if segment.atlasflow_run_id != run_id:
            raise ValueError("segment.atlasflow_run_id must match the target run")
        async with self._lock:
            record = self._get(run_id)
            segments = list(record.trace_segments)
            for index, current in enumerate(segments):
                if current.segment_id == segment.segment_id:
                    if current == segment:
                        return
                    segments[index] = segment.model_copy(update={"started_at": current.started_at})
                    break
            else:
                segments.append(segment)
            self._runs[run_id] = self._validated_update(record, trace_segments=segments)

    async def list(
        self,
        *,
        query: str | None = None,
        status: RunStatus | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[RunRecord], int]:
        async with self._lock:
            needle = query.casefold() if query else None
            records = [
                record
                for record in self._runs.values()
                if (
                    needle is None
                    or needle in record.query.casefold()
                    or needle in record.id.casefold()
                )
                and (status is None or record.status is status)
                and (date_from is None or record.created_at >= date_from)
                and (date_to is None or record.created_at <= date_to)
            ]
            records.sort(key=lambda item: item.created_at, reverse=True)
            total = len(records)
            start = (page - 1) * page_size
            return (
                [item.model_copy(deep=True) for item in records[start : start + page_size]],
                total,
            )

    async def records_since(self, since: datetime | None) -> list[RunRecord]:
        async with self._lock:
            records = [
                record
                for record in self._runs.values()
                if since is None or record.created_at >= since
            ]
            return [
                item.model_copy(deep=True)
                for item in sorted(records, key=lambda value: value.created_at)
            ]

    async def delete(self, run_id: str) -> None:
        async with self._lock:
            record = self._get(run_id)
            if record.status not in RunStatus.terminal():
                raise InvalidRunStateError(
                    "Only terminal runs can be deleted; cancel the run first"
                )
            del self._runs[run_id]

    async def ensure_conversation_writable(self, conversation_id: str) -> None:
        # This legacy store has no conversation repository. Fail closed rather
        # than allowing an unverifiable conversation-owned execution to start.
        raise InvalidRunStateError("Conversation execution requires SQLite conversation storage")

    def _get(self, run_id: str) -> RunRecord:
        record = self._runs.get(run_id)
        if record is None:
            raise RunNotFoundError(run_id)
        return record

    @staticmethod
    def _validated_update(record: RunRecord, **changes: object) -> RunRecord:
        payload = record.model_dump(mode="python")
        payload.update(changes)
        payload["updated_at"] = utc_now()
        return RunRecord.model_validate(payload)


class RunService:
    def __init__(self, store: RunStore, workflow: ResearchWorkflow, *,
                 execution: ExecutionRuntime | None = None,
                 snapshot_factory: Callable[[RunPolicy], Awaitable[dict[str, Any] | None]] | None = None) -> None:
        self.store = store
        self.workflow = workflow
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.execution = execution
        self.snapshot_factory = snapshot_factory
        self._operation_locks: dict[str, asyncio.Lock] = {}
        if execution is not None:
            execution.bind_store(store)

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        return self._operation_locks.setdefault(run_id, asyncio.Lock())

    async def _ensure_conversation_writable(self, run: RunRecord) -> None:
        if run.conversation_id is not None:
            await self.store.ensure_conversation_writable(run.conversation_id)

    async def _bind_new(self, run: RunRecord) -> None:
        snapshot = None
        if self.snapshot_factory is not None and run.policy.knowledge_mode != "off":
            snapshot = await self.snapshot_factory(run.policy)
            await self.store.update(run.id, retrieval_snapshot=snapshot)
        if self.execution is not None:
            await self.execution.bind_run(run.id, tool_limit=run.policy.max_tool_calls_per_run,
                                          retrieval_snapshot=snapshot)
            await self.execution.claim(run.id, request_id=f"initial:{run.id}",
                                       payload={"kind": "initial"})

    async def create(
        self,
        query: str,
        *,
        auto_approve: bool = True,
        policy: RunPolicy | None = None,
    ) -> RunRecord:
        resolved_policy = policy or RunPolicy()
        run = await self.store.create(
            query,
            auto_approve=auto_approve,
            policy=resolved_policy,
        )
        await self._bind_new(run)
        self._track(
            run.id,
            self._execute_start(
                run.id,
                query,
                auto_approve=auto_approve,
                policy=resolved_policy,
            ),
        )
        return run

    async def rerun(
        self,
        source_run_id: str,
        *,
        query: str | None = None,
        auto_approve: bool | None = None,
        policy: RunPolicy | None = None,
    ) -> RunRecord:
        source = await self.store.get(source_run_id)
        await self._ensure_conversation_writable(source)
        resolved_query = query if query is not None else source.query
        resolved_auto_approve = auto_approve if auto_approve is not None else source.auto_approve
        resolved_policy = policy or source.policy
        run = await self.store.create(
            resolved_query,
            auto_approve=resolved_auto_approve,
            policy=resolved_policy,
            source_run_id=source.id,
        )
        await self._bind_new(run)
        self._track(
            run.id,
            self._execute_start(
                run.id,
                resolved_query,
                auto_approve=resolved_auto_approve,
                policy=resolved_policy,
            ),
        )
        return run

    async def launch_prepared(self, run_id: str) -> RunRecord:
        """Dispatch a conversation-reserved Run; never allocate another identity."""
        async with self._run_lock(run_id):
            run = await self.store.get(run_id)
            await self._ensure_conversation_writable(run)
            if run.status is not RunStatus.PENDING or run_id in self._tasks:
                return run
            try:
                await self._bind_new(run)
            except ExecutionControlError as exc:
                await self._interrupt_execution(run_id, exc)
                return await self.store.get(run_id)
            except Exception as exc:  # noqa: BLE001 - preserve the reserved Run on failure.
                await self._fail(run_id, exc)
                return await self.store.get(run_id)
            self._track(run_id, self._execute_start(run_id, run.query,
                auto_approve=run.auto_approve, policy=run.policy))
            return await self.store.get(run_id)

    async def execute_and_wait(
        self,
        query: str,
        *,
        auto_approve: bool = True,
        policy: RunPolicy | None = None,
    ) -> RunRecord:
        resolved_policy = policy or RunPolicy()
        run = await self.store.create(
            query,
            auto_approve=auto_approve,
            policy=resolved_policy,
        )
        await self._bind_new(run)
        await self._execute_start(
            run.id,
            query,
            auto_approve=auto_approve,
            policy=resolved_policy,
        )
        return await self.store.get(run.id)

    async def resolve_approval(self, run_id: str, request: ApprovalRequest) -> RunRecord:
        async with self._run_lock(run_id):
            return await self._resolve_approval_locked(run_id, request)

    async def _resolve_approval_locked(self, run_id: str, request: ApprovalRequest) -> RunRecord:
        run = await self.store.get(run_id)
        await self._ensure_conversation_writable(run)
        if self.execution is not None:
            request_id = request.request_id or str(uuid4())
            payload = {"kind": "approval", "action": request.action.value,
                       "edited_plan": request.edited_plan.model_dump(mode="json") if request.edited_plan else None,
                       "expected_checkpoint_id": request.expected_checkpoint_id}
            bundle = await self.execution.repository.read(run_id)
            if bundle and request_id in bundle.commands:
                from atlasflow.execution.contracts import digest
                if bundle.commands[request_id]["hash"] != digest(payload):
                    raise ExecutionConflict("request_id reused with a different payload")
                return run
        if run.status is not RunStatus.WAITING_APPROVAL:
            raise InvalidRunStateError(
                f"Run {run_id} is {run.status.value}; approval requires waiting_approval"
            )
        if request.edited_plan is not None:
            self.workflow.validate_plan_capabilities(request.edited_plan, run.policy)
        if self.execution is not None:
            recovery = await self.inspect_recovery(run_id)
            if request.expected_checkpoint_id and request.expected_checkpoint_id != recovery.checkpoint_id:
                raise ExecutionConflict("checkpoint changed; reload approval")
            await self.execution.claim(run_id, request_id=request_id, payload=payload,
                                       checkpoint_id=recovery.checkpoint_id)
        await self.store.transition(
            run_id,
            RunStatus.RUNNING,
        )
        self._track(
            run_id,
            self._execute_resume(
                run_id,
                action=request.action.value,
                edited_plan=request.edited_plan,
            ),
        )
        return await self.store.get(run_id)

    async def cancel(self, run_id: str, *, request_id: str | None = None) -> RunRecord:
        async with self._run_lock(run_id):
            return await self._cancel_locked(run_id, request_id=request_id)

    async def _cancel_locked(self, run_id: str, *, request_id: str | None = None) -> RunRecord:
        run = await self.store.get(run_id)
        if self.execution is not None and request_id:
            bundle = await self.execution.repository.read(run_id)
            if bundle and request_id in bundle.commands:
                from atlasflow.execution.contracts import decode_safe
                if decode_safe(bundle.commands[request_id]["payload"]).get("kind") != "cancel":
                    raise ExecutionConflict("request_id reused with a different payload")
                return run
        if run.status in RunStatus.terminal():
            raise InvalidRunStateError(
                f"Run {run_id} is already {run.status.value}; it cannot be cancelled"
            )
        if self.execution is not None:
            async with self.execution.delivery_lock(run_id):
                fresh = await self.store.get(run_id)
                if fresh.status in RunStatus.terminal():
                    raise InvalidRunStateError("Run reached a terminal state before cancellation")
                bundle = await self.execution.repository.read(run_id)
                if bundle is not None:
                    await self.execution.cancel(run_id, request_id=request_id, under_delivery_lock=True)
                elif fresh.status not in {RunStatus.PENDING, RunStatus.INTERRUPTED}:
                    raise ExecutionConflict("active Run execution binding missing; cancellation blocked")
                # A reserved Run can be cancelled before its first binding, or
                # after startup marked that never-dispatched reservation interrupted.
                # There is no durable work/outbox to fence in that case.

        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if self.workflow.tool_runtime is not None:
            self.workflow.tool_runtime.clear_run(run_id)
        current = await self.store.get(run_id)
        if current.status in RunStatus.terminal():
            if current.status is RunStatus.CANCELLED:
                return current
            raise InvalidRunStateError(
                f"Run {run_id} became {current.status.value} before cancellation"
            )
        return await self.store.transition(
            run_id,
            RunStatus.CANCELLED,
            event=RunEvent(
                run_id=run_id,
                event_type=RunEventType.RUN_CANCELLED,
                message="任务已由用户取消",
                status=RunStatus.CANCELLED.value,
                data={"reason": "user_cancelled"},
            ),
        )

    async def delete(self, run_id: str, confirmation_run_id: str) -> None:
        if confirmation_run_id != run_id:
            raise InvalidRunStateError("Run ID confirmation does not match")
        async with self._run_lock(run_id):
            run = await self.store.get(run_id)
            if run.status not in RunStatus.terminal():
                raise InvalidRunStateError("Only terminal runs can be deleted")
            if self.execution is not None:
                bundle = await self.execution.repository.read(run_id)
                if bundle is not None:
                    # Tombstone/fence first. Old outbox must never recreate or
                    # prevent startup after the SQLite cascade deletion.
                    await self.execution.cancel(run_id, deleted=True)
            await self.store.delete(run_id)

    @asynccontextmanager
    async def conversation_deletion(self, conversation_id: str, run_ids: Sequence[str]) -> AsyncIterator[None]:
        """Stop and fence an exact owned set; keep Run locks through SQL commit.

        Conversation storage performs the physical multi-Run delete atomically.
        No source-run traversal: a separately rerun report is not owned here.
        """

        async with AsyncExitStack() as stack:
            ids = sorted(set(run_ids))
            for run_id in ids:
                await stack.enter_async_context(self._run_lock(run_id))
            for run_id in ids:
                try:
                    run = await self.store.get(run_id)
                except RunNotFoundError:
                    run = None  # A previously deleted linked Run is harmless.
                if run is not None and run.conversation_id != conversation_id:
                    raise InvalidRunStateError("Run ownership does not match the conversation; deletion blocked")
                if run is not None and run.status not in RunStatus.terminal():
                    try:
                        await self._cancel_locked(run_id)
                    except InvalidRunStateError:
                        # Completion may race cancellation. Terminal is safe;
                        # any other invalid state must abort physical deletion.
                        if (await self.store.get(run_id)).status not in RunStatus.terminal():
                            raise
                task = self._tasks.get(run_id)
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if self.execution is not None:
                    bundle = await self.execution.repository.read(run_id)
                    if bundle is not None:
                        await self.execution.cancel(run_id, deleted=True)
                if self.workflow.tool_runtime is not None:
                    self.workflow.tool_runtime.clear_run(run_id)
            yield

    async def inspect_recovery(self, run_id: str) -> RecoveryInfo:
        if self.execution is None:
            return RecoveryInfo(reason="persistent execution is not enabled")
        bundle = await self.execution.repository.read(run_id)
        if bundle is None:
            return RecoveryInfo(reason="no persistent checkpoint; create a new Run",
                                blocked_by="missing_binding")
        snapshot = await self.workflow.graph.aget_state(self.workflow._config(run_id))
        checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
        unknown = [UnknownCall(call_id=key, kind=bundle.invocations[key]["kind"],
                    name=bundle.invocations[key]["name"],
                    tool_budget_counted=bundle.invocations[key].get("tool_budget_counted", False),
                    usage=bundle.invocations[key].get("usage"))
                   for key in self.execution._active_unknown(bundle)]
        reason = None
        if bundle.control.get("deleted") or bundle.control.get("cancel_requested"):
            reason = "Run cancelled or deleted"
        elif bundle.control["profile_hash"] != self.execution.profile_hash:
            reason = "execution configuration incompatible; create a new Run"
        elif not snapshot.values:
            reason = "no persistent checkpoint; create a new Run"
        elif bundle.control.get("owner"):
            reason = "execution already active"
        elif snapshot.interrupts and self.execution.pending_approval(bundle, checkpoint_id) is None:
            reason = "waiting for approval"
        elif not snapshot.next:
            reason = "Run has reached a terminal checkpoint"
        return RecoveryInfo(available=reason is None, reason=reason,
                            checkpoint_id=checkpoint_id, unknown_calls=unknown,
                            recovery_count=bundle.control.get("recovery_count", 0),
                            blocked_by=reason,
                            provider_usage=self.execution.provider_summary(bundle))

    async def resume(self, run_id: str, request: ResumeRunRequest) -> RunRecord:
        if self.execution is None:
            raise ExecutionConflict("persistent execution is not enabled")
        async with self._run_lock(run_id):
            run = await self.store.get(run_id)
            await self._ensure_conversation_writable(run)
            payload = {"kind": "resume", **request.model_dump()}
            bundle = await self.execution.repository.read(run_id)
            if bundle and request.request_id in bundle.commands:
                from atlasflow.execution.contracts import digest
                if bundle.commands[request.request_id]["hash"] != digest(payload):
                    raise ExecutionConflict("request_id reused with a different payload")
                return run
            if run.status is not RunStatus.INTERRUPTED:
                raise ExecutionConflict("only an interrupted Run can be continued")
            recovery = await self.inspect_recovery(run_id)
            if not recovery.available:
                raise ExecutionConflict(recovery.reason or "Run cannot be resumed")
            if request.expected_checkpoint_id != recovery.checkpoint_id:
                raise ExecutionConflict("checkpoint changed; reload before continuing")
            claim, repeated = await self.execution.claim(
                run_id, request_id=request.request_id, payload=payload,
                acknowledged=request.acknowledged_unknown_call_ids, recovery=True,
                checkpoint_id=recovery.checkpoint_id)
            if repeated:
                return await self.store.get(run_id)
            event = RunEvent(run_id=run_id, event_type=RunEventType.RUN_RESUMED,
                             message="从持久化检查点继续执行", status=RunStatus.RUNNING.value)
            await self.execution.emit(run_id, event,
                patch={"status": RunStatus.RUNNING, "recovery": RecoveryInfo()},
                logical_key=f"resume:{request.request_id}")
            pending = self.execution.pending_approval(bundle, recovery.checkpoint_id)
            self._track(run_id, self._execute_continue(run_id, claim, pending))
            return await self.store.get(run_id)

    async def _execute_continue(self, run_id: str, claim: Any, pending_approval: Any = None) -> None:
        try:
            if pending_approval is not None:
                _, approved = pending_approval
                execution = await self.workflow.resume(run_id=run_id, action=approved["action"],
                    edited_plan=approved["edited_plan"], recovery_resume=True)
            else:
                execution = await self.workflow.continue_run(run_id=run_id)
            await self._apply_execution(run_id, execution)
            await self.execution.complete_command(claim,
                recovered_approval=pending_approval[0] if pending_approval else None)
        except ExecutionControlError as exc:
            await self._interrupt_execution(run_id, exc)
        except Exception as exc:  # noqa: BLE001 - workflow failure is projected, not retried.
            await self._fail(run_id, exc)
        finally:
            await self.execution.release(claim)

    async def _interrupt_execution(self, run_id: str, exc: BaseException) -> None:
        run = await self.store.get(run_id)
        if run.status in RunStatus.terminal():
            return
        if self.execution is not None:
            try:
                await self.execution.recover_owner(run_id)
            except PersistenceUnavailable:
                # PG may itself be down: stop dispatch, preserve the SQLite
                # interrupted projection; next successful startup reconciles it.
                pass
        await self.store.transition(run_id, RunStatus.INTERRUPTED,
            recovery=RecoveryInfo(reason=str(exc)),
            event=RunEvent(run_id=run_id, event_type=RunEventType.RUN_INTERRUPTED,
                           message="执行中断，已停止新的外部请求", data={"reason": str(exc)},
                           status=RunStatus.INTERRUPTED.value))

    async def reconcile_startup(self) -> None:
        if self.execution is None:
            return
        # No graph execution occurs here. Read-only graph snapshots determine the
        # projection; external work requires a subsequent explicit user command.
        for run_id in await self.execution.repository.run_ids():
            bundle = await self.execution.recover_owner(run_id)
            if bundle.control.get("deleted"):
                try:
                    await self.store.delete(run_id)
                except RunNotFoundError:
                    pass
                continue
            try:
                run = await self.store.get(run_id)
            except RunNotFoundError:
                await self.execution.cancel(run_id, deleted=True)
                continue
            await self.execution.flush_run(run_id)
            run = await self.store.get(run_id)
            if run.status in RunStatus.terminal():
                continue
            if bundle.control.get("cancel_requested"):
                await self.store.transition(run_id, RunStatus.CANCELLED,
                    event=RunEvent(run_id=run_id, event_type=RunEventType.RUN_CANCELLED,
                                   message="恢复已保存的取消意图"))
                continue
            snapshot = await self.workflow.graph.aget_state(self.workflow._config(run_id))
            if not snapshot.values:
                await self.store.transition(run_id, RunStatus.FAILED,
                    error="No persistent checkpoint is available; create a new Run",
                    event=RunEvent(run_id=run_id, event_type=RunEventType.RUN_FAILED,
                                   message="无可用持久化检查点，无法继续此运行"))
                continue
            final_status = snapshot.values.get("final_status")
            if final_status in {status.value for status in RunStatus.terminal()}:
                await self._apply_execution(run_id, WorkflowExecution(
                    state=dict(snapshot.values), final=True))
            elif snapshot.interrupts and self.execution.pending_approval(
                    bundle, (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")) is None:
                await self.store.update(run_id, **self._artifact_changes(dict(snapshot.values)),
                    status=RunStatus.WAITING_APPROVAL, recovery=await self.inspect_recovery(run_id))
            else:
                info = await self.inspect_recovery(run_id)
                await self.store.update(run_id, **self._artifact_changes(dict(snapshot.values)),
                                        status=RunStatus.INTERRUPTED, recovery=info)
        # Pre-upgrade unfinished Runs have no graph checkpoint. Never reconstruct
        # them from events or silently restart their external work.
        for run in await self.store.records_since(None):
            if run.status in RunStatus.terminal():
                continue
            if await self.execution.repository.read(run.id) is None:
                await self.store.transition(run.id, RunStatus.FAILED,
                    error="Legacy in-memory execution has no durable checkpoint; create a new Run",
                    event=RunEvent(run_id=run.id, event_type=RunEventType.RUN_FAILED,
                                   message="旧运行无持久化检查点，请新建运行"))

    async def close(self) -> None:
        tasks = list(self._tasks.items())
        for _, task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        if self.execution is not None:
            for run_id, _ in tasks:
                try:
                    await self._interrupt_execution(run_id, RuntimeError("service shutdown"))
                except RunNotFoundError:
                    pass

    async def analytics(self, selected_range: Literal["7d", "30d", "all"]) -> AnalyticsResponse:
        now = utc_now()
        since = {
            "7d": now - timedelta(days=7),
            "30d": now - timedelta(days=30),
            "all": None,
        }[selected_range]
        records = await self.store.records_since(since)

        status_counts = Counter(record.status for record in records)
        successful = sum(
            status_counts[status]
            for status in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS)
        )
        terminal_runs = sum(status_counts[status] for status in RunStatus.terminal())
        durations = [
            record.metrics.duration_ms
            for record in records
            if record.metrics.duration_ms is not None
        ]
        successful_tasks = sum(record.metrics.successful_tasks for record in records)
        failed_tasks = sum(record.metrics.failed_tasks for record in records)
        partial_tasks = sum(record.metrics.partial_tasks for record in records)
        attempted_tasks = successful_tasks + failed_tasks + partial_tasks
        quality_scores = [
            record.quality_history[-1].score for record in records if record.quality_history
        ]
        duration_by_date: dict[str, list[int]] = defaultdict(list)
        decision_counts: Counter[str] = Counter()
        version_counts: Counter[int] = Counter()
        for record in records:
            if record.metrics.duration_ms is not None:
                duration_by_date[record.created_at.date().isoformat()].append(
                    record.metrics.duration_ms
                )
            decision_counts.update(decision.decision.value for decision in record.critique_history)
            decision_counts.update(decision.decision.value for decision in record.quality_history)
            if record.plan is not None:
                version_counts[record.plan.plan_version] += 1

        return AnalyticsResponse(
            range=selected_range,
            summary=AnalyticsSummary(
                total_runs=len(records),
                success_rate=(successful / terminal_runs * 100 if terminal_runs else 0.0),
                task_success_rate=(
                    successful_tasks / attempted_tasks * 100 if attempted_tasks else 0.0
                ),
                avg_quality_score=(
                    sum(quality_scores) / len(quality_scores) if quality_scores else None
                ),
                avg_duration_ms=(sum(durations) / len(durations) if durations else None),
                total_model_calls=sum(record.metrics.model_calls for record in records),
                total_tasks=sum(record.metrics.total_tasks for record in records),
                successful_tasks=successful_tasks,
                failed_tasks=failed_tasks,
                partial_tasks=partial_tasks,
                total_supplement_rounds=sum(record.metrics.supplement_rounds for record in records),
                total_replans=sum(record.metrics.replan_count for record in records),
                total_revisions=sum(record.metrics.revision_count for record in records),
            ),
            status_distribution=[
                StatusCount(status=status, count=status_counts[status])
                for status in RunStatus
                if status_counts[status]
            ],
            duration_trend=[
                DurationTrendPoint(
                    date=date,
                    avg_duration_ms=sum(values) / len(values),
                    runs=len(values),
                )
                for date, values in sorted(duration_by_date.items())
            ],
            decision_counts=[
                DecisionCount(decision=decision, count=count)
                for decision, count in sorted(decision_counts.items())
            ],
            plan_versions=[
                PlanVersionCount(version=version, count=count)
                for version, count in sorted(version_counts.items())
            ],
        )

    def _track(self, run_id: str, coroutine: Any) -> None:
        active = self._tasks.get(run_id)
        if active is not None and not active.done():
            coroutine.close()
            raise InvalidRunStateError("Run already has an active task")
        task = asyncio.create_task(coroutine)
        self._tasks[run_id] = task

        def forget(completed: asyncio.Task[None]) -> None:
            if self._tasks.get(run_id) is completed:
                self._tasks.pop(run_id, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(forget)

    async def _execute_start(
        self,
        run_id: str,
        query: str,
        *,
        auto_approve: bool,
        policy: RunPolicy,
    ) -> None:
        await self.store.transition(
            run_id,
            RunStatus.RUNNING,
            event=RunEvent(
                run_id=run_id,
                event_type=RunEventType.RUN_STARTED,
                message="任务开始执行",
                status=RunStatus.RUNNING.value,
            ),
        )
        try:
            run = await self.store.get(run_id)
            execution = await self.workflow.start(
                run_id=run_id,
                query=query,
                auto_approve=auto_approve,
                policy=policy,
                retrieval_snapshot=run.retrieval_snapshot,
                **({"continuation_input": run.continuation_input} if run.continuation_input else {}),
            )
            await self._apply_execution(run_id, execution)
            if self.execution is not None:
                await self.execution.complete_command(self.execution.claims[run_id])
        except ExecutionControlError as exc:
            await self._interrupt_execution(run_id, exc)
        except Exception as exc:  # noqa: BLE001
            await self._fail(run_id, exc)
        finally:
            if self.execution is not None and run_id in self.execution.claims:
                await self.execution.release(self.execution.claims[run_id])

    async def _execute_resume(
        self, run_id: str, *, action: str, edited_plan: object | None
    ) -> None:
        try:
            execution = await self.workflow.resume(
                run_id=run_id,
                action=action,
                edited_plan=edited_plan,
            )
            await self._apply_execution(run_id, execution)
            if self.execution is not None:
                await self.execution.complete_command(self.execution.claims[run_id])
        except ExecutionControlError as exc:
            await self._interrupt_execution(run_id, exc)
        except Exception as exc:  # noqa: BLE001
            await self._fail(run_id, exc)
        finally:
            if self.execution is not None and run_id in self.execution.claims:
                await self.execution.release(self.execution.claims[run_id])

    async def _apply_execution(self, run_id: str, execution: WorkflowExecution) -> None:
        changes = self._artifact_changes(execution.state)
        # Include schema-repair requests hidden inside a gateway method span.
        snapshot = await self.store.get(run_id)
        actual_calls = [call for segment in snapshot.trace_segments
                        for call in segment.provider_calls if call.operation == "chat"]
        durable_summary = None
        if self.execution is not None:
            bundle = await self.execution.repository.read(run_id)
            durable_summary = self.execution.provider_summary(bundle) if bundle else None
        if durable_summary and durable_summary["model_calls"] and "metrics" in changes:
            changes["metrics"] = execution.state["metrics"].model_copy(
                update={"model_calls": durable_summary["model_calls"]})
        elif actual_calls and "metrics" in changes:
            changes["metrics"] = execution.state["metrics"].model_copy(
                update={"model_calls": len({call.call_id for call in actual_calls})}
            )
        if execution.paused:
            await self.store.transition(
                run_id,
                RunStatus.WAITING_APPROVAL,
                **changes,
            )
            return
        if not execution.final:
            raise RuntimeError("Workflow returned without pausing or reaching a terminal state")

        status = RunStatus(execution.status)
        event_type, message = {
            RunStatus.COMPLETED: (RunEventType.RUN_COMPLETED, "任务执行完成"),
            RunStatus.COMPLETED_WITH_WARNINGS: (
                RunEventType.RUN_DEGRADED,
                "任务以警告状态完成",
            ),
            RunStatus.FAILED: (RunEventType.RUN_FAILED, "任务执行失败"),
            RunStatus.CANCELLED: (RunEventType.RUN_CANCELLED, "任务已取消"),
        }[status]
        if status is RunStatus.FAILED:
            changes["error"] = self._failure_message(execution.state)
        if status is RunStatus.COMPLETED_WITH_WARNINGS and not changes.get("warnings"):
            changes["warnings"] = ["质量预算耗尽，报告可能存在未解决问题"]
        if self.execution is not None:
            event = RunEvent(run_id=run_id, event_type=event_type, message=message,
                             status=status.value)
            await self.execution.emit(run_id, event, patch={**changes, "status": status},
                                      logical_key="lifecycle:terminal")
            return
        await self.store.transition(
            run_id,
            status,
            event=RunEvent(
                run_id=run_id,
                event_type=event_type,
                message=message,
                status=status.value,
            ),
            **changes,
        )

    async def _fail(self, run_id: str, exc: Exception) -> None:
        if self.workflow.tool_runtime is not None:
            self.workflow.tool_runtime.clear_run(run_id)
        run = await self.store.get(run_id)
        if run.status in RunStatus.terminal():
            return
        await self.store.transition(
            run_id,
            RunStatus.FAILED,
            error=str(exc),
            event=RunEvent(
                run_id=run_id,
                event_type=RunEventType.RUN_FAILED,
                message="任务执行失败",
                status=RunStatus.FAILED.value,
                data={"error": str(exc)},
            ),
        )

    @staticmethod
    def _artifact_changes(state: dict[str, Any]) -> dict[str, object]:
        keys = (
            "retrieval_snapshot",
            "policy",
            "plan",
            "plans",
            "plan_lineage",
            "plan_lineages",
            "research_results",
            "tool_calls",
            "evidence",
            "critique_history",
            "review_contexts",
            "draft_versions",
            "quality_history",
            "route_history",
            "trace_segments",
            "errors",
            "metrics",
            "report",
            "warnings",
        )
        changes = {key: state[key] for key in keys if key in state}
        if state.get("final_status") and "final_warnings" in state:
            changes["warnings"] = list(state["final_warnings"])
        return changes

    @staticmethod
    def _failure_message(state: dict[str, Any]) -> str:
        if state.get("error"):
            return str(state["error"])
        errors = state.get("errors") or []
        if errors:
            last = errors[-1]
            return str(getattr(last, "message", last))
        return "Agent workflow failed"
