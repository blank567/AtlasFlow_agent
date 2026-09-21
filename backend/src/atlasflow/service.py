from __future__ import annotations

import asyncio
from typing import Any

from atlasflow.agents.workflow import ResearchWorkflow, WorkflowExecution
from atlasflow.schemas import (
    ApprovalRequest,
    RunEvent,
    RunEventType,
    RunRecord,
    RunStatus,
    utc_now,
)


class RunNotFoundError(KeyError):
    pass


class InvalidRunStateError(RuntimeError):
    pass


_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_WARNINGS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED}
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.COMPLETED_WITH_WARNINGS: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class InMemoryRunStore:
    """Process-local repository used until the persistence phase of v0.2."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, query: str, *, auto_approve: bool = True) -> RunRecord:
        record = RunRecord(query=query, auto_approve=auto_approve)
        async with self._lock:
            self._runs[record.id] = record
        return record.model_copy(deep=True)

    async def get(self, run_id: str) -> RunRecord:
        async with self._lock:
            return self._get(run_id).model_copy(deep=True)

    async def append_event(self, run_id: str, event: RunEvent) -> None:
        async with self._lock:
            record = self._get(run_id)
            sequenced = event.model_copy(update={"sequence": len(record.events) + 1})
            updated = self._validated_update(record, events=[*record.events, sequenced])
            self._runs[run_id] = updated

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

        async with self._lock:
            record = self._get(run_id)
            if (
                status is not record.status
                and status not in _ALLOWED_TRANSITIONS[record.status]
            ):
                raise InvalidRunStateError(
                    f"Cannot transition run {run_id} from {record.status.value} to {status.value}"
                )
            events = list(record.events)
            if event is not None:
                events.append(event.model_copy(update={"sequence": len(events) + 1}))
            updated = self._validated_update(
                record, status=status, events=events, **changes
            )
            self._runs[run_id] = updated
            return updated.model_copy(deep=True)

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
    def __init__(self, store: InMemoryRunStore, workflow: ResearchWorkflow) -> None:
        self.store = store
        self.workflow = workflow
        self._tasks: set[asyncio.Task[None]] = set()

    async def create(self, query: str, *, auto_approve: bool = True) -> RunRecord:
        run = await self.store.create(query, auto_approve=auto_approve)
        self._track(self._execute_start(run.id, query, auto_approve=auto_approve))
        return run

    async def execute_and_wait(
        self, query: str, *, auto_approve: bool = True
    ) -> RunRecord:
        run = await self.store.create(query, auto_approve=auto_approve)
        await self._execute_start(run.id, query, auto_approve=auto_approve)
        return await self.store.get(run.id)

    async def resolve_approval(
        self, run_id: str, request: ApprovalRequest
    ) -> RunRecord:
        run = await self.store.get(run_id)
        if run.status is not RunStatus.WAITING_APPROVAL:
            raise InvalidRunStateError(
                f"Run {run_id} is {run.status.value}; approval requires waiting_approval"
            )
        await self.store.transition(
            run_id,
            RunStatus.RUNNING,
        )
        self._track(
            self._execute_resume(
                run_id,
                action=request.action.value,
                edited_plan=request.edited_plan,
            )
        )
        return await self.store.get(run_id)

    def _track(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _execute_start(
        self, run_id: str, query: str, *, auto_approve: bool
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
            execution = await self.workflow.start(
                run_id=run_id,
                query=query,
                auto_approve=auto_approve,
            )
            await self._apply_execution(run_id, execution)
        except Exception as exc:  # noqa: BLE001
            await self._fail(run_id, exc)

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
        except Exception as exc:  # noqa: BLE001
            await self._fail(run_id, exc)

    async def _apply_execution(self, run_id: str, execution: WorkflowExecution) -> None:
        changes = self._artifact_changes(execution.state)
        if execution.paused:
            await self.store.transition(
                run_id,
                RunStatus.WAITING_APPROVAL,
                **changes,
            )
            return
        if not execution.final:
            raise RuntimeError(
                "Workflow returned without pausing or reaching a terminal state"
            )

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
            "plan",
            "plans",
            "research_results",
            "critique_history",
            "draft_versions",
            "quality_history",
            "route_history",
            "errors",
            "metrics",
            "report",
            "warnings",
        )
        return {key: state[key] for key in keys if key in state}

    @staticmethod
    def _failure_message(state: dict[str, Any]) -> str:
        if state.get("error"):
            return str(state["error"])
        errors = state.get("errors") or []
        if errors:
            last = errors[-1]
            return str(getattr(last, "message", last))
        return "Agent workflow failed"
