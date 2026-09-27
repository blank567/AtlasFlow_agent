from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.workflow import ResearchWorkflow, WorkflowExecution
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
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
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

    async def upsert_trace_segment(self, run_id: str, segment: TraceSegment) -> None: ...


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
    def __init__(self, store: RunStore, workflow: ResearchWorkflow) -> None:
        self.store = store
        self.workflow = workflow
        self._tasks: dict[str, asyncio.Task[None]] = {}

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
        resolved_query = query if query is not None else source.query
        resolved_auto_approve = auto_approve if auto_approve is not None else source.auto_approve
        resolved_policy = policy or source.policy
        run = await self.store.create(
            resolved_query,
            auto_approve=resolved_auto_approve,
            policy=resolved_policy,
            source_run_id=source.id,
        )
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
        await self._execute_start(
            run.id,
            query,
            auto_approve=auto_approve,
            policy=resolved_policy,
        )
        return await self.store.get(run.id)

    async def resolve_approval(self, run_id: str, request: ApprovalRequest) -> RunRecord:
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
            run_id,
            self._execute_resume(
                run_id,
                action=request.action.value,
                edited_plan=request.edited_plan,
            ),
        )
        return await self.store.get(run_id)

    async def cancel(self, run_id: str) -> RunRecord:
        run = await self.store.get(run_id)
        if run.status in RunStatus.terminal():
            raise InvalidRunStateError(
                f"Run {run_id} is already {run.status.value}; it cannot be cancelled"
            )

        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if self.workflow.tool_runtime is not None:
            self.workflow.tool_runtime.budget.clear(run_id)
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
        await self.store.delete(run_id)

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
        attempted_tasks = successful_tasks + failed_tasks
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
            execution = await self.workflow.start(
                run_id=run_id,
                query=query,
                auto_approve=auto_approve,
                policy=policy,
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
            self.workflow.tool_runtime.budget.clear(run_id)
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
