from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from atlasflow.agents.contracts import RunPolicy
from atlasflow.observability import TraceSegment
from atlasflow.schemas import RunEvent, RunEventType, RunRecord, RunStatus, utc_now
from atlasflow.service import InvalidRunStateError, RunNotFoundError

_ACTIVE_STATUSES = (
    RunStatus.PENDING,
    RunStatus.RUNNING,
    RunStatus.WAITING_APPROVAL,
)


class SQLiteRunStore:
    """Single-process SQLite repository with an append-only event log.

    ``runs`` stores immutable identity/search fields, ``run_projections`` stores the
    latest validated RunRecord snapshot, and ``run_events`` stores ordered events.
    The connection is intentionally process-local; the application does not claim
    multi-process writer support.
    """

    def __init__(self, database_path: str | Path) -> None:
        raw_path = str(database_path)
        self.database_path = raw_path
        if raw_path != ":memory:":
            path = Path(raw_path).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.database_path = str(path)

        self._connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.database_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._lock = asyncio.Lock()
        self._initialize_schema()
        self._recover_interrupted_runs()

    def _initialize_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    source_run_id TEXT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_projections (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    auto_approve INTEGER NOT NULL,
                    policy_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_runs_created_at
                    ON runs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_source_run_id
                    ON runs(source_run_id);
                CREATE INDEX IF NOT EXISTS idx_projections_status_updated
                    ON run_projections(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_run_sequence
                    ON run_events(run_id, sequence);
                """
            )

    def _recover_interrupted_runs(self) -> None:
        placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
        rows = self._connection.execute(
            f"""
            SELECT p.run_id, p.snapshot_json
            FROM run_projections AS p
            WHERE p.status IN ({placeholders})
            """,
            tuple(status.value for status in _ACTIVE_STATUSES),
        ).fetchall()
        if not rows:
            return

        recovered_at = utc_now()
        error = (
            "Backend restarted before this run reached a terminal state; "
            "in-memory workflow execution cannot be resumed"
        )
        with self._connection:
            for row in rows:
                run_id = str(row["run_id"])
                snapshot = json.loads(str(row["snapshot_json"]))
                snapshot.update(
                    {
                        "status": RunStatus.FAILED.value,
                        "error": error,
                        "updated_at": recovered_at.isoformat(),
                    }
                )
                record = RunRecord.model_validate_json(
                    json.dumps({**snapshot, "events": []}, ensure_ascii=False)
                )
                event = RunEvent(
                    run_id=run_id,
                    event_type=RunEventType.RUN_FAILED,
                    message="服务重启，未完成任务已终止",
                    status=RunStatus.FAILED.value,
                    created_at=recovered_at,
                    data={"reason": "backend_restart", "error": error},
                )
                sequence = self._next_sequence_sync(run_id)
                self._insert_event_sync(event.model_copy(update={"sequence": sequence}))
                self._update_projection_sync(record)

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
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO runs(id, query, source_run_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.query,
                        record.source_run_id,
                        record.created_at.isoformat(),
                    ),
                )
                self._insert_projection_sync(record)
        return record.model_copy(deep=True)

    async def get(self, run_id: str) -> RunRecord:
        async with self._lock:
            return self._get_sync(run_id).model_copy(deep=True)

    async def append_event(self, run_id: str, event: RunEvent) -> None:
        if event.run_id != run_id:
            raise ValueError("event.run_id must match the target run")
        async with self._lock:
            with self._connection:
                record = self._get_sync(run_id)
                duplicate = self._connection.execute(
                    "SELECT 1 FROM run_events WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                if duplicate is not None:
                    return
                sequenced = event.model_copy(
                    update={"sequence": self._next_sequence_sync(run_id)}
                )
                self._insert_event_sync(sequenced)
                self._update_projection_sync(
                    self._validated_update(record, events=record.events)
                )

    async def events_after(
        self, run_id: str, after_sequence: int
    ) -> list[RunEvent]:
        async with self._lock:
            self._require_run_sync(run_id)
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (run_id, after_sequence),
            ).fetchall()
            return [
                RunEvent.model_validate_json(str(row["payload_json"])) for row in rows
            ]

    async def event_batch(
        self, run_id: str, after_sequence: int
    ) -> tuple[list[RunEvent], RunStatus]:
        """Read an SSE event batch and projection status from one lock snapshot."""

        async with self._lock:
            row = self._connection.execute(
                "SELECT status FROM run_projections WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RunNotFoundError(run_id)
            event_rows = self._connection.execute(
                """
                SELECT payload_json
                FROM run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (run_id, after_sequence),
            ).fetchall()
            return (
                [
                    RunEvent.model_validate_json(str(event_row["payload_json"]))
                    for event_row in event_rows
                ],
                RunStatus(str(row["status"])),
            )

    async def update(self, run_id: str, **changes: object) -> RunRecord:
        async with self._lock:
            with self._connection:
                record = self._get_sync(run_id)
                updated = self._validated_update(record, **changes)
                self._update_projection_sync(updated)
            return updated.model_copy(deep=True)

    async def transition(
        self,
        run_id: str,
        status: RunStatus,
        *,
        event: RunEvent | None = None,
        **changes: object,
    ) -> RunRecord:
        if event is not None and event.run_id != run_id:
            raise ValueError("event.run_id must match the target run")
        async with self._lock:
            with self._connection:
                record = self._get_sync(run_id)
                self._validate_transition(record, status)
                if event is not None:
                    duplicate = self._connection.execute(
                        "SELECT 1 FROM run_events WHERE event_id = ?",
                        (event.event_id,),
                    ).fetchone()
                    if duplicate is None:
                        event = event.model_copy(
                            update={"sequence": self._next_sequence_sync(run_id)}
                        )
                        self._insert_event_sync(event)
                updated = self._validated_update(
                    record,
                    status=status,
                    events=record.events,
                    **changes,
                )
                self._update_projection_sync(updated)
            return self._get_sync(run_id).model_copy(deep=True)

    async def upsert_trace_segment(
        self, run_id: str, segment: TraceSegment
    ) -> None:
        """Persist the running/final forms of one invocation as one segment."""

        if segment.atlasflow_run_id != run_id:
            raise ValueError("segment.atlasflow_run_id must match the target run")
        async with self._lock:
            with self._connection:
                record = self._get_sync(run_id)
                segments = list(record.trace_segments)
                for index, current in enumerate(segments):
                    if current.segment_id != segment.segment_id:
                        continue
                    if current == segment:
                        return
                    if current.status != "running" and segment.status == "running":
                        return
                    segments[index] = segment.model_copy(
                        update={"started_at": current.started_at}
                    )
                    break
                else:
                    segments.append(segment)

                stored_segment = next(
                    item
                    for item in segments
                    if item.segment_id == segment.segment_id
                )
                event = RunEvent(
                    run_id=run_id,
                    event_type=RunEventType.TRACE_SEGMENT_UPDATED,
                    message={
                        "running": "LangSmith Trace Segment 已开始",
                        "completed": "LangSmith Trace Segment 已完成",
                        "failed": "LangSmith Trace Segment 执行失败",
                    }[stored_segment.status],
                    agent="observability",
                    status=stored_segment.status,
                    data={"segment": stored_segment.model_dump(mode="json")},
                ).model_copy(update={"sequence": self._next_sequence_sync(run_id)})
                self._insert_event_sync(event)
                self._update_projection_sync(
                    self._validated_update(
                        record,
                        events=record.events,
                        trace_segments=segments,
                    )
                )

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
        if page < 1 or page_size < 1:
            raise ValueError("page and page_size must be positive")
        where, parameters = self._list_filters(
            query=query,
            status=status,
            date_from=date_from,
            date_to=date_to,
        )
        offset = (page - 1) * page_size
        async with self._lock:
            total_row = self._connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM runs AS r
                JOIN run_projections AS p ON p.run_id = r.id
                {where}
                """,
                parameters,
            ).fetchone()
            rows = self._connection.execute(
                f"""
                SELECT r.id
                FROM runs AS r
                JOIN run_projections AS p ON p.run_id = r.id
                {where}
                ORDER BY r.created_at DESC, r.id DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, page_size, offset),
            ).fetchall()
            items = [
                self._get_sync(str(row["id"]), include_events=False) for row in rows
            ]
        return [item.model_copy(deep=True) for item in items], int(total_row["count"])

    async def records_since(self, since: datetime | None) -> list[RunRecord]:
        async with self._lock:
            if since is None:
                rows = self._connection.execute(
                    "SELECT id FROM runs ORDER BY created_at ASC, id ASC"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT id FROM runs
                    WHERE created_at >= ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (since.isoformat(),),
                ).fetchall()
            return [
                self._get_sync(str(row["id"]), include_events=False).model_copy(
                    deep=True
                )
                for row in rows
            ]

    async def delete(self, run_id: str) -> None:
        async with self._lock:
            with self._connection:
                record = self._get_sync(run_id)
                if record.status not in RunStatus.terminal():
                    raise InvalidRunStateError(
                        "Only terminal runs can be deleted; cancel the run first"
                    )
                self._connection.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    async def close(self) -> None:
        async with self._lock:
            self._connection.close()

    @property
    def size_bytes(self) -> int:
        if self.database_path == ":memory:":
            return 0
        path = Path(self.database_path)
        return sum(
            candidate.stat().st_size
            for candidate in (
                path,
                path.with_name(f"{path.name}-wal"),
                path.with_name(f"{path.name}-shm"),
            )
            if candidate.exists()
        )

    def _get_sync(self, run_id: str, *, include_events: bool = True) -> RunRecord:
        row = self._connection.execute(
            """
            SELECT p.snapshot_json
            FROM run_projections AS p
            WHERE p.run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        payload = json.loads(str(row["snapshot_json"]))
        payload["events"] = (
            [
                json.loads(str(event_row["payload_json"]))
                for event_row in self._connection.execute(
                    """
                    SELECT payload_json FROM run_events
                    WHERE run_id = ? ORDER BY sequence ASC
                    """,
                    (run_id,),
                ).fetchall()
            ]
            if include_events
            else []
        )
        return RunRecord.model_validate_json(json.dumps(payload, ensure_ascii=False))

    def _require_run_sync(self, run_id: str) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)

    def _insert_projection_sync(self, record: RunRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO run_projections(
                run_id, status, auto_approve, policy_json, snapshot_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.status.value,
                int(record.auto_approve),
                json.dumps(record.policy.model_dump(mode="json"), ensure_ascii=False),
                self._snapshot_json(record),
                record.updated_at.isoformat(),
            ),
        )

    def _update_projection_sync(self, record: RunRecord) -> None:
        result = self._connection.execute(
            """
            UPDATE run_projections
            SET status = ?, auto_approve = ?, policy_json = ?,
                snapshot_json = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (
                record.status.value,
                int(record.auto_approve),
                json.dumps(record.policy.model_dump(mode="json"), ensure_ascii=False),
                self._snapshot_json(record),
                record.updated_at.isoformat(),
                record.id,
            ),
        )
        if result.rowcount == 0:
            raise RunNotFoundError(record.id)

    def _insert_event_sync(self, event: RunEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO run_events(
                run_id, sequence, event_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.run_id,
                event.sequence,
                event.event_id,
                event.event_type.value,
                event.model_dump_json(),
                event.created_at.isoformat(),
            ),
        )

    def _next_sequence_sync(self, run_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM run_events WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        return int(row["next_sequence"])

    @staticmethod
    def _snapshot_json(record: RunRecord) -> str:
        payload = record.model_dump(mode="json", exclude={"events"})
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _validated_update(record: RunRecord, **changes: object) -> RunRecord:
        payload = record.model_dump(mode="python")
        payload.update(changes)
        payload["updated_at"] = utc_now()
        return RunRecord.model_validate(payload)

    @staticmethod
    def _validate_transition(record: RunRecord, status: RunStatus) -> None:
        from atlasflow.service import allowed_transitions

        if status is not record.status and status not in allowed_transitions(
            record.status
        ):
            raise InvalidRunStateError(
                f"Cannot transition run {record.id} from "
                f"{record.status.value} to {status.value}"
            )

    @staticmethod
    def _list_filters(
        *,
        query: str | None,
        status: RunStatus | None,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> tuple[str, Sequence[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(r.query LIKE ? ESCAPE '\\' OR r.id LIKE ? ESCAPE '\\')")
            pattern = f"%{escaped}%"
            parameters.extend((pattern, pattern))
        if status is not None:
            clauses.append("p.status = ?")
            parameters.append(status.value)
        if date_from is not None:
            clauses.append("r.created_at >= ?")
            parameters.append(date_from.isoformat())
        if date_to is not None:
            clauses.append("r.created_at <= ?")
            parameters.append(date_to.isoformat())
        return (
            f"WHERE {' AND '.join(clauses)}" if clauses else "",
            tuple(parameters),
        )
