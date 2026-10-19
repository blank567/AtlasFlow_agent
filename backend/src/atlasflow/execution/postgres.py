"""PostgreSQL execution repository and the official asynchronous saver.

Constructors are connection-free. start verifies pre-existing tables; schema
initialization is an explicit command, never an application startup mutation.
"""
# Driver boundaries deliberately replace all private driver messages with safe errors.
# ruff: noqa: BLE001
from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any, TypeVar

from sqlalchemy import Column, MetaData, String, Table, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from atlasflow.execution.contracts import ExecutionConflict, PersistenceUnavailable, digest
from atlasflow.execution.ports import RunBundle
from atlasflow.execution.runtime import ExecutionRuntime

T = TypeVar("T")
DEFAULT_SCHEMA = "atlasflow_execution"


def execution_tables(schema: str = DEFAULT_SCHEMA) -> tuple[MetaData, dict[str, Table]]:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise ValueError("invalid execution schema")
    metadata = MetaData(schema=schema)
    tables = {"control": Table("execution_controls", metadata,
                              Column("run_id", String(36), primary_key=True),
                              Column("payload", JSONB, nullable=False))}
    for name, table_name in (("invocations", "execution_invocations"),
                             ("commands", "execution_commands"),
                             ("outbox", "execution_outbox")):
        tables[name] = Table(table_name, metadata,
                             Column("run_id", String(36), primary_key=True),
                             Column("item_id", String(256), primary_key=True),
                             Column("payload", JSONB, nullable=False))
    return metadata, tables


class PostgresExecutionRepository:
    def __init__(self, database_url: str, *, schema: str = DEFAULT_SCHEMA) -> None:
        self.metadata, self.tables = execution_tables(schema)
        self.schema = schema
        parsed = make_url(database_url).set(drivername="postgresql+asyncpg")
        self.engine = create_async_engine(parsed, pool_pre_ping=True)
        self.owner_connection: Any = None

    async def start(self) -> None:
        try:
            # A held advisory lock enforces the advertised single-process limit.
            self.owner_connection = await self.engine.connect()
            key = int(digest({"schema": self.schema})[:15], 16)
            owned = await self.owner_connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            if not owned:
                raise ExecutionConflict("another application process owns execution storage")
            for table in self.tables.values():
                await self.owner_connection.execute(select(table.c.payload).limit(1))
            # Official saver tables are initialized by init_execution_store.py.
            for name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                exists = await self.owner_connection.scalar(
                    text("SELECT to_regclass(:table)"), {"table": f"{self.schema}.{name}"})
                if not exists:
                    raise ExecutionConflict("execution storage not initialized; run the explicit initializer")
        except ExecutionConflict:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise PersistenceUnavailable("PostgreSQL execution storage is unavailable or unmigrated") from None

    async def close(self) -> None:
        if self.owner_connection is not None:
            await self.owner_connection.close()
            self.owner_connection = None
        await self.engine.dispose()

    async def bind(self, run_id: str, control: dict[str, Any]) -> None:
        try:
            async with self.engine.begin() as connection:
                await connection.execute(insert(self.tables["control"]).values(
                    run_id=run_id, payload=control).on_conflict_do_nothing())
        except Exception:
            raise PersistenceUnavailable("execution binding could not be committed") from None

    async def _load(self, connection: Any, run_id: str, *, lock: bool = False) -> RunBundle | None:
        table = self.tables["control"]
        query = select(table.c.payload).where(table.c.run_id == run_id)
        if lock:
            query = query.with_for_update()
        control = (await connection.execute(query)).scalar_one_or_none()
        if control is None:
            return None
        values: dict[str, Any] = {}
        for name in ("invocations", "commands", "outbox"):
            table = self.tables[name]
            rows = await connection.execute(select(table.c.item_id, table.c.payload)
                                             .where(table.c.run_id == run_id)
                                             .order_by(table.c.item_id))
            values[name] = {key: payload for key, payload in rows}
        return RunBundle(control, **values)

    async def read(self, run_id: str) -> RunBundle | None:
        try:
            async with self.engine.connect() as connection:
                return await self._load(connection, run_id)
        except Exception:
            raise PersistenceUnavailable("execution storage could not be read") from None

    async def run_ids(self) -> list[str]:
        try:
            async with self.engine.connect() as connection:
                return list((await connection.scalars(select(self.tables["control"].c.run_id))).all())
        except Exception:
            raise PersistenceUnavailable("execution storage could not be enumerated") from None

    async def mutate(self, run_id: str, operation: Callable[[RunBundle], T]) -> T:
        try:
            async with self.engine.begin() as connection:
                bundle = await self._load(connection, run_id, lock=True)
                if bundle is None:
                    raise ExecutionConflict("execution binding not found")
                before = deepcopy(bundle)
                result = operation(bundle)
                table = self.tables["control"]
                await connection.execute(update(table).where(table.c.run_id == run_id)
                                         .values(payload=bundle.control))
                for name in ("invocations", "commands", "outbox"):
                    table = self.tables[name]
                    for key, payload in getattr(bundle, name).items():
                        if getattr(before, name).get(key) == payload:
                            continue
                        await connection.execute(insert(table).values(run_id=run_id,
                            item_id=key, payload=payload).on_conflict_do_update(
                                index_elements=[table.c.run_id, table.c.item_id],
                                set_={"payload": payload}))
                return result
        except ExecutionConflict:
            raise
        except Exception as exc:
            from atlasflow.execution.contracts import ExecutionControlError
            from atlasflow.execution.runtime import ExecutionToolLimit
            if isinstance(exc, (ExecutionControlError, ExecutionToolLimit)):
                raise
            raise PersistenceUnavailable("execution transaction could not be committed") from None


def create_postgres_execution(database_url: str, *, profile_hash: str = "research-v1",
                              schema: str = DEFAULT_SCHEMA) -> ExecutionRuntime:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    from atlasflow.execution.contracts import checkpoint_msgpack_modules

    repository = PostgresExecutionRepository(database_url, schema=schema)
    parsed = make_url(database_url).set(drivername="postgresql")
    pool = AsyncConnectionPool(parsed.render_as_string(hide_password=False), open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                "options": f"-c search_path={schema},public"}, min_size=1, max_size=5)
    allowed = checkpoint_msgpack_modules()

    class SafePostgresSaver(AsyncPostgresSaver):
        async def aget_tuple(self, *args, **kwargs):
            try:
                return await super().aget_tuple(*args, **kwargs)
            except Exception:
                raise PersistenceUnavailable("checkpoint could not be read") from None

        async def aput(self, *args, **kwargs):
            try:
                return await super().aput(*args, **kwargs)
            except Exception:
                raise PersistenceUnavailable("checkpoint could not be committed") from None

        async def aput_writes(self, *args, **kwargs):
            try:
                return await super().aput_writes(*args, **kwargs)
            except Exception:
                raise PersistenceUnavailable("checkpoint writes could not be committed") from None

    serializer = JsonPlusSerializer(allowed_msgpack_modules=allowed)
    official_saver = None

    class LoopSafeSaver(BaseCheckpointSaver):
        """Psycopg requires Selector on Windows, without changing the app loop.

        The official saver/pool run together in one dedicated selector thread.
        Main-loop HTTP/asyncpg and subprocess support remain untouched.
        """
        def __init__(self):
            super().__init__(serde=serializer)
            self.conn = pool
            self._loop = None
            self._thread = None

        async def _run(self, coroutine):
            import asyncio
            import sys
            from threading import Thread
            if sys.platform != "win32":
                return await coroutine
            if self._loop is None:
                self._loop = asyncio.SelectorEventLoop()
                self._thread = Thread(target=self._loop.run_forever,
                                      name="atlasflow-checkpoint-selector", daemon=True)
                self._thread.start()
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, self._loop))

        async def open(self):
            async def initialize():
                nonlocal official_saver
                if official_saver is None:
                    # Official saver captures its running loop at construction.
                    # Build it here, on the same loop as its connection pool.
                    official_saver = SafePostgresSaver(pool, serde=serializer)
                await pool.open(wait=True, timeout=10)
            await self._run(initialize())

        async def aclose(self):
            import asyncio
            if self._loop is None:
                await pool.close()
                return
            await self._run(pool.close())
            self._loop.call_soon_threadsafe(self._loop.stop)
            await asyncio.to_thread(self._thread.join, 5)
            self._loop.close()
            self._loop = None
            self._thread = None

        async def setup(self):
            await self._run(official_saver.setup())

        async def aget_tuple(self, *args, **kwargs):
            return await self._run(official_saver.aget_tuple(*args, **kwargs))

        async def aput(self, *args, **kwargs):
            return await self._run(official_saver.aput(*args, **kwargs))

        async def aput_writes(self, *args, **kwargs):
            return await self._run(official_saver.aput_writes(*args, **kwargs))

        async def alist(self, *args, **kwargs):
            async def collect():
                return [item async for item in official_saver.alist(*args, **kwargs)]
            for item in await self._run(collect()):
                yield item

        async def adelete_thread(self, thread_id):
            return await self._run(official_saver.adelete_thread(thread_id))

        def get_next_version(self, current, channel):
            if official_saver is None:
                raise PersistenceUnavailable("checkpoint saver has not been started")
            return official_saver.get_next_version(current, channel)

    saver = LoopSafeSaver()

    class PostgresRuntime(ExecutionRuntime):
        async def start(self):
            try:
                await repository.start()
                await saver.open()
                self.ready = True
            except BaseException:
                await saver.aclose()
                await repository.close()
                raise

        async def close(self):
            await saver.aclose()
            await super().close()

    return PostgresRuntime(repository, saver, profile_hash=profile_hash)
