from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path, PureWindowsPath


class V2EvaluationStore:
    """Own tables, immutable artifacts, and no v1 result migration or rewriting."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if PureWindowsPath(self.path).drive.upper() == "C:":
            raise ValueError("evaluation storage must not use drive C")
        self._db: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        async with self._lock:
            if self._db is not None:
                return
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, timeout=10)
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS evaluation_v2_experiments (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evaluation_v2_inputs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS ix_evaluation_v2_experiments_kind
                    ON evaluation_v2_experiments(kind, created_at);
                CREATE INDEX IF NOT EXISTS ix_evaluation_v2_inputs_kind
                    ON evaluation_v2_inputs(kind, created_at);
            """)
            self._db.commit()

    def _connection(self):
        if self._db is None:
            raise RuntimeError("v2 evaluation store is not initialized")
        return self._db

    async def close(self):
        async with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    async def save(self, value: dict):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        async with self._lock:
            db = self._connection()
            with db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT kind, payload FROM evaluation_v2_experiments WHERE id=?",
                    (value["experiment_id"],),
                ).fetchone()
                if existing and existing[0] != value["kind"]:
                    raise ValueError("experiment kind cannot change")
                if existing and json.loads(existing[1]).get("deleted_at"):
                    raise ValueError("deleted experiments cannot be overwritten")
                db.execute(
                    "INSERT INTO evaluation_v2_experiments VALUES (?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (value["experiment_id"], value["kind"], value["created_at"], payload),
                )

    async def get(self, experiment_id: str, kind: str | None = None, *, include_deleted=False):
        async with self._lock:
            row = self._connection().execute(
                "SELECT payload FROM evaluation_v2_experiments WHERE id=?", (experiment_id,)
            ).fetchone()
        if row is None:
            raise KeyError("v2 experiment not found")
        value = json.loads(row[0])
        if kind is not None and value["kind"] != kind:
            raise KeyError("experiment not found in this workspace")
        if value.get("deleted_at") and not include_deleted:
            raise KeyError("v2 experiment not found")
        return value

    async def delete(self, experiment_id: str, kind: str | None, deleted_at: str):
        """Tombstone a terminal experiment; never remove shared evidence artifacts."""
        async with self._lock:
            db = self._connection()
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT payload FROM evaluation_v2_experiments WHERE id=?", (experiment_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("v2 experiment not found")
                value = json.loads(row[0])
                if kind is not None and value["kind"] != kind:
                    raise KeyError("experiment not found in this workspace")
                if not value.get("deleted_at"):
                    if value["status"] in {"planned", "running"}:
                        raise ValueError("active experiments must be cancelled before deletion")
                    value["deleted_at"] = deleted_at
                    db.execute(
                        "UPDATE evaluation_v2_experiments SET payload=? WHERE id=?",
                        (json.dumps(value, ensure_ascii=False, allow_nan=False), experiment_id),
                    )
        return {"experiment_id": experiment_id, "kind": value["kind"], "status": "deleted",
                "deleted_at": value["deleted_at"], "artifacts_retained": True}

    async def list(self, kind: str | None = None, limit: int = 100):
        sql = "SELECT payload FROM evaluation_v2_experiments WHERE json_extract(payload, '$.deleted_at') IS NULL"
        params: list = []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        async with self._lock:
            rows = self._connection().execute(sql, params).fetchall()
        return [json.loads(row[0]) for row in rows]

    async def list_summaries(self, kind: str | None = None, limit: int = 100):
        """Return the fields needed for the history list, without result payloads."""
        fields = (
            "experiment_id", "kind", "protocol", "plan", "dataset_hash", "status",
            "created_at", "ended_at", "summary", "budget_ledger", "stop_reason", "sync_status",
        )
        columns = ", ".join(f"json_extract(payload, '$.{field}')" for field in fields)
        sql = f"SELECT {columns} FROM evaluation_v2_experiments WHERE json_extract(payload, '$.deleted_at') IS NULL"
        params: list = []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        async with self._lock:
            rows = self._connection().execute(sql, params).fetchall()
        json_fields = {"plan", "summary", "budget_ledger"}
        return [
            {field: json.loads(value) if field in json_fields and value is not None else value for field, value in zip(fields, row)}
            for row in rows
        ]

    async def register_input(self, value: dict):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        async with self._lock:
            db = self._connection()
            with db:
                row = db.execute(
                    "SELECT payload FROM evaluation_v2_inputs WHERE id=?", (value["input_id"],)
                ).fetchone()
                if row:
                    if json.loads(row[0]) != value:
                        raise ValueError("frozen artifacts cannot be overwritten")
                    return json.loads(row[0])
                db.execute(
                    "INSERT INTO evaluation_v2_inputs VALUES (?,?,?,?)",
                    (value["input_id"], value["kind"], value["created_at"], encoded),
                )
        return value

    async def get_input(self, input_id: str):
        async with self._lock:
            row = self._connection().execute(
                "SELECT payload FROM evaluation_v2_inputs WHERE id=?", (input_id,)
            ).fetchone()
        if row is None:
            raise KeyError("v2 frozen artifact not found")
        return json.loads(row[0])

    async def list_inputs(self, kind: str | None = None, limit: int = 2000):
        sql = "SELECT payload FROM evaluation_v2_inputs"
        params: list = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        async with self._lock:
            rows = self._connection().execute(sql, params).fetchall()
        return [json.loads(row[0]) for row in rows]
