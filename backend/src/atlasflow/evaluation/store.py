from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from pathlib import Path, PureWindowsPath

from atlasflow.evaluation.contracts import EvaluationExperiment, FrozenEvaluationInput


class EvaluationStore:
    """Separate local evaluation database; immutable inputs, durable partial results."""

    def __init__(self, path: str | Path, artifacts_dir: str | Path | None = None):
        self.path = str(path)
        self.artifacts_dir = (
            Path(artifacts_dir)
            if artifacts_dir
            else (Path(path).parent / "artifacts" if self.path != ":memory:" else None)
        )
        for target in (self.path, str(self.artifacts_dir or "")):
            if PureWindowsPath(target).drive.upper() == "C:":
                raise ValueError("evaluation storage must not use drive C")
        self._db: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._db is not None:
                return
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path)
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS evaluation_experiments (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evaluation_inputs (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            self._db.commit()

    async def close(self) -> None:
        async with self._lock:
            if self._db:
                self._db.close()
                self._db = None

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("evaluation store is not initialized")
        return self._db

    async def save(self, value: EvaluationExperiment) -> None:
        async with self._lock:
            db = self._connection()
            db.execute(
                "INSERT INTO evaluation_experiments VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (value.experiment_id, value.created_at.isoformat(), value.model_dump_json()),
            )
            db.commit()
            if self.artifacts_dir:
                folder = self.artifacts_dir / value.experiment_id
                folder.mkdir(parents=True, exist_ok=True)
                self._write_json(
                    folder / "manifest.json", value.model_dump(mode="json", exclude={"results"})
                )
                self._write_json(folder / "summary.json", value.summary)
                self._write_lines(
                    folder / "cases.jsonl", [row.model_dump(mode="json") for row in value.results]
                )
                self._write_lines(
                    folder / "provider_calls.jsonl",
                    [call for row in value.results for call in row.provider_calls],
                )

    @staticmethod
    def _write_json(path: Path, value) -> None:
        EvaluationStore._atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))

    @staticmethod
    def _write_lines(path: Path, values: list) -> None:
        EvaluationStore._atomic_write(
            path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in values)
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".eval-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def get(self, experiment_id: str) -> EvaluationExperiment:
        async with self._lock:
            row = (
                self._connection()
                .execute("SELECT payload FROM evaluation_experiments WHERE id=?", (experiment_id,))
                .fetchone()
            )
        if row is None:
            raise KeyError("evaluation experiment not found")
        return EvaluationExperiment.model_validate_json(row[0])

    async def list(self, limit: int = 100) -> list[EvaluationExperiment]:
        async with self._lock:
            rows = (
                self._connection()
                .execute(
                    "SELECT payload FROM evaluation_experiments ORDER BY created_at DESC LIMIT ?",
                    (min(max(limit, 1), 200),),
                )
                .fetchall()
            )
        return [EvaluationExperiment.model_validate_json(row[0]) for row in rows]

    async def register_input(self, value: FrozenEvaluationInput) -> FrozenEvaluationInput:
        async with self._lock:
            db = self._connection()
            existing = db.execute(
                "SELECT payload FROM evaluation_inputs WHERE id=?", (value.input_id,)
            ).fetchone()
            if existing:
                saved = FrozenEvaluationInput.model_validate_json(existing[0])
                if saved != value:
                    raise ValueError("frozen inputs cannot be overwritten")
                return saved
            db.execute(
                "INSERT INTO evaluation_inputs VALUES (?, ?, ?)",
                (value.input_id, value.created_at.isoformat(), value.model_dump_json()),
            )
            db.commit()
        return value

    async def get_input(self, input_id: str) -> FrozenEvaluationInput:
        async with self._lock:
            row = (
                self._connection()
                .execute("SELECT payload FROM evaluation_inputs WHERE id=?", (input_id,))
                .fetchone()
            )
        if row is None:
            raise KeyError("frozen evaluation input not found")
        return FrozenEvaluationInput.model_validate_json(row[0])

    async def list_inputs(self, limit: int = 1000) -> list[FrozenEvaluationInput]:
        async with self._lock:
            rows = (
                self._connection()
                .execute(
                    "SELECT payload FROM evaluation_inputs ORDER BY created_at DESC LIMIT ?",
                    (min(limit, 1000),),
                )
                .fetchall()
            )
        return [FrozenEvaluationInput.model_validate_json(row[0]) for row in rows]
