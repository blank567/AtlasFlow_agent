from __future__ import annotations

import asyncio

from atlasflow.agents import ResearchWorkflow
from atlasflow.schemas import RunEvent, RunRecord, RunStatus, utc_now


class RunNotFoundError(KeyError):
    pass


class InMemoryRunStore:
    """Replaceable demo store. PostgreSQL persistence is the v0.2 boundary."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, query: str) -> RunRecord:
        record = RunRecord(query=query)
        async with self._lock:
            self._runs[record.id] = record
        return record.model_copy(deep=True)

    async def get(self, run_id: str) -> RunRecord:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise RunNotFoundError(run_id)
            return record.model_copy(deep=True)

    async def append_event(self, run_id: str, event: RunEvent) -> None:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise RunNotFoundError(run_id)
            record.events.append(event)
            record.updated_at = utc_now()

    async def update(self, run_id: str, **changes: object) -> RunRecord:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise RunNotFoundError(run_id)
            updated = record.model_copy(update={**changes, "updated_at": utc_now()})
            self._runs[run_id] = updated
            return updated.model_copy(deep=True)


class RunService:
    def __init__(self, store: InMemoryRunStore, workflow: ResearchWorkflow) -> None:
        self.store = store
        self.workflow = workflow
        self._tasks: set[asyncio.Task[None]] = set()

    async def create(self, query: str) -> RunRecord:
        run = await self.store.create(query)
        task = asyncio.create_task(self._execute(run.id, query))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def execute_and_wait(self, query: str) -> RunRecord:
        run = await self.store.create(query)
        await self._execute(run.id, query)
        return await self.store.get(run.id)

    async def _execute(self, run_id: str, query: str) -> None:
        await self.store.update(run_id, status=RunStatus.RUNNING)
        await self.store.append_event(
            run_id, RunEvent(type="lifecycle", message="任务开始执行")
        )
        try:
            state = await self.workflow.run(run_id=run_id, query=query)
            await self.store.update(
                run_id,
                status=RunStatus.COMPLETED,
                plan=state.get("plan", []),
                evidence=state.get("evidence", []),
                tool_calls=state.get("tool_calls", []),
                critiques=state.get("critiques", []),
                report=state.get("report", ""),
            )
            await self.store.append_event(
                run_id, RunEvent(type="lifecycle", message="任务执行完成")
            )
        # This is the background-task boundary: every failure must become persisted run state.
        except Exception as exc:  # noqa: BLE001
            await self.store.update(run_id, status=RunStatus.FAILED, error=str(exc))
            await self.store.append_event(
                run_id, RunEvent(type="error", message="任务执行失败", data={"error": str(exc)})
            )
