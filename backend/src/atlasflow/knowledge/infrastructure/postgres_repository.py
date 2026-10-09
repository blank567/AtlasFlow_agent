from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypeVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    delete,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from atlasflow.knowledge.domain import (
    DocumentStatus,
    DocumentVersion,
    IndexGeneration,
    IngestionJob,
    JobStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeFilter,
    KnowledgeSpace,
)
from atlasflow.knowledge.infrastructure.memory_repository import _matches

T = TypeVar("T")
metadata = MetaData()

spaces = Table(
    "knowledge_spaces",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("slug", String(63), unique=True, nullable=False),
    Column("active_generation_id", String(36)),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
documents = Table(
    "knowledge_documents",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("space_id", String(36), nullable=False, index=True),
    Column("source_id", String(300), nullable=False),
    Column("current_version_id", String(36)),
    Column("status", String(32), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
versions = Table(
    "knowledge_document_versions",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("document_id", String(36), nullable=False, index=True),
    Column("content_hash", String(64), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
generations = Table(
    "knowledge_index_generations",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("space_id", String(36), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
chunks = Table(
    "knowledge_chunks_v2",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("document_id", String(36), nullable=False, index=True),
    Column("version_id", String(36), nullable=False, index=True),
    Column("generation_id", String(36), nullable=False, index=True),
    Column("chunk_kind", String(16), nullable=False, index=True),
    Column("content", Text, nullable=False),
    Column("lexical_text", Text, nullable=False),
    Column("embedding", Vector(), nullable=True),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
jobs = Table(
    "knowledge_jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("space_id", String(36), nullable=False, index=True),
    Column("document_id", String(36), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class PostgresKnowledgeRepository:
    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)

    async def initialize(self) -> None:
        # Schema changes are owned by Alembic. Startup only verifies that the
        # migrated knowledge tables are reachable; it must not mutate schema.
        try:
            async with self.engine.connect() as connection:
                await connection.execute(select(spaces.c.id).limit(1))
        except OSError as exc:
            host = self.engine.url.host or "configured host"
            port = self.engine.url.port or 5432
            raise RuntimeError(
                f"KNOWLEDGE_DATABASE_UNAVAILABLE: PostgreSQL at {host}:{port} is not reachable. "
                "Start Docker Desktop, run 'docker-compose up -d postgres', "
                "then run 'python -m alembic upgrade head' before starting the API."
            ) from exc

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_space(self, value: KnowledgeSpace) -> KnowledgeSpace:
        await self._insert(
            spaces,
            value,
            slug=value.slug,
            active_generation_id=value.active_generation_id,
        )
        return value

    async def list_spaces(self) -> list[KnowledgeSpace]:
        return await self._list(spaces, KnowledgeSpace)

    async def get_space(self, value: str) -> KnowledgeSpace | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(spaces.c.payload).where(
                        (spaces.c.id == value) | (spaces.c.slug == value)
                    )
                )
            ).first()
        return KnowledgeSpace.model_validate(row.payload) if row else None

    async def update_space(self, value: KnowledgeSpace) -> KnowledgeSpace:
        await self._update(
            spaces,
            value,
            active_generation_id=value.active_generation_id,
            slug=value.slug,
        )
        return value

    async def create_document(self, value: KnowledgeDocument) -> KnowledgeDocument:
        if await self.find_document(value.space_id, value.source_id):
            raise ValueError(f"document source already exists: {value.source_id}")
        await self._insert(
            documents,
            value,
            space_id=value.space_id,
            source_id=value.source_id,
            current_version_id=value.current_version_id,
            status=value.status.value,
        )
        return value

    async def update_document(self, value: KnowledgeDocument) -> KnowledgeDocument:
        await self._update(
            documents,
            value,
            status=value.status.value,
            current_version_id=value.current_version_id,
        )
        return value

    async def get_document(self, document_id: str) -> KnowledgeDocument | None:
        return await self._get(documents, document_id, KnowledgeDocument)

    async def find_document(
        self, space_id: str, source_id: str
    ) -> KnowledgeDocument | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(documents.c.payload).where(
                        documents.c.space_id == space_id,
                        documents.c.source_id == source_id,
                    )
                )
            ).first()
        return KnowledgeDocument.model_validate(row.payload) if row else None

    async def list_documents(self, space_id: str) -> list[KnowledgeDocument]:
        return await self._list(
            documents, KnowledgeDocument, documents.c.space_id == space_id
        )

    async def purge_document(self, document_id: str) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                delete(chunks).where(chunks.c.document_id == document_id)
            )
            await connection.execute(
                delete(jobs).where(jobs.c.document_id == document_id)
            )
            await connection.execute(
                delete(versions).where(versions.c.document_id == document_id)
            )
            await connection.execute(
                delete(documents).where(documents.c.id == document_id)
            )

    async def create_version(self, value: DocumentVersion) -> DocumentVersion:
        await self._insert(
            versions,
            value,
            document_id=value.document_id,
            content_hash=value.content_hash,
        )
        return value

    async def update_version(self, value: DocumentVersion) -> DocumentVersion:
        await self._update(versions, value)
        return value

    async def get_version(self, version_id: str) -> DocumentVersion | None:
        return await self._get(versions, version_id, DocumentVersion)

    async def list_versions(self, document_id: str) -> list[DocumentVersion]:
        values = await self._list(
            versions, DocumentVersion, versions.c.document_id == document_id
        )
        return sorted(values, key=lambda item: item.version_number)

    async def create_generation(self, value: IndexGeneration) -> IndexGeneration:
        await self._insert(
            generations, value, space_id=value.space_id, status=value.status
        )
        return value

    async def get_generation(self, generation_id: str) -> IndexGeneration | None:
        return await self._get(generations, generation_id, IndexGeneration)

    async def active_generation(self, space_id: str) -> IndexGeneration | None:
        space = await self.get_space(space_id)
        return (
            await self.get_generation(space.active_generation_id)
            if space and space.active_generation_id
            else None
        )

    async def activate_generation(self, generation_id: str) -> IndexGeneration:
        selected = await self.get_generation(generation_id)
        if not selected:
            raise KeyError(generation_id)
        now = datetime.now(UTC)
        previous = await self._list(
            generations,
            IndexGeneration,
            (generations.c.space_id == selected.space_id)
            & (generations.c.status == "active"),
        )
        for item in previous:
            await self._update(
                generations,
                item.model_copy(update={"status": "retired"}),
                status="retired",
            )
        active = selected.model_copy(update={"status": "active", "activated_at": now})
        await self._update(generations, active, status="active")
        space = await self.get_space(selected.space_id)
        if not space:
            raise RuntimeError("generation references missing space")
        await self.update_space(
            space.model_copy(
                update={"active_generation_id": active.id, "updated_at": now}
            )
        )
        return active

    async def replace_chunks(
        self, version_id: str, values: list[KnowledgeChunk]
    ) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                delete(chunks).where(chunks.c.version_id == version_id)
            )
            if values:
                await connection.execute(
                    chunks.insert(),
                    [
                        {
                            "id": item.id,
                            "document_id": item.document_id,
                            "version_id": item.version_id,
                            "generation_id": item.generation_id,
                            "chunk_kind": item.chunk_kind,
                            "content": item.content,
                            "lexical_text": item.lexical_text,
                            "embedding": item.embedding,
                            "payload": item.model_dump(
                                mode="json", exclude={"embedding"}
                            ),
                            "created_at": item.created_at,
                        }
                        for item in values
                    ],
                )

    async def get_chunks(self, chunk_ids: list[str]) -> list[KnowledgeChunk]:
        if not chunk_ids:
            return []
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(chunks.c.payload, chunks.c.embedding).where(
                        chunks.c.id.in_(chunk_ids)
                    )
                )
            ).all()
        return [_chunk_from_row(row) for row in rows]

    async def count_version_chunks(self, version_id: str, kind: str = "child") -> int:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(chunks.c.id).where(
                        chunks.c.version_id == version_id, chunks.c.chunk_kind == kind
                    )
                )
            ).all()
        return len(rows)

    async def list_searchable_chunks(
        self, space_id: str, filters: KnowledgeFilter | None = None
    ) -> list[KnowledgeChunk]:
        space = await self.get_space(space_id)
        if not space or not space.active_generation_id:
            return []
        statement = (
            select(chunks.c.payload, chunks.c.embedding)
            .select_from(chunks.join(documents, chunks.c.document_id == documents.c.id))
            .where(
                chunks.c.generation_id == space.active_generation_id,
                chunks.c.chunk_kind == "child",
                documents.c.status == DocumentStatus.ACTIVE.value,
                chunks.c.version_id == documents.c.current_version_id,
            )
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).all()
        values = [_chunk_from_row(row) for row in rows]
        return [item for item in values if _matches(item, filters)]

    async def create_job(self, value: IngestionJob) -> IngestionJob:
        await self._insert(
            jobs,
            value,
            space_id=value.space_id,
            document_id=value.document_id,
            status=value.status.value,
        )
        return value

    async def update_job(self, value: IngestionJob) -> IngestionJob:
        await self._update(jobs, value, status=value.status.value)
        return value

    async def get_job(self, job_id: str) -> IngestionJob | None:
        return await self._get(jobs, job_id, IngestionJob)

    async def list_jobs(self, space_id: str | None = None) -> list[IngestionJob]:
        condition = jobs.c.space_id == space_id if space_id else None
        values = await self._list(jobs, IngestionJob, condition)
        return sorted(values, key=lambda item: item.created_at, reverse=True)

    async def claim_job(self, job_id: str | None = None) -> IngestionJob | None:
        async with self.engine.begin() as connection:
            statement = select(jobs.c.payload).where(
                jobs.c.status.in_(["queued", "failed_retryable"])
            )
            if job_id is not None:
                statement = statement.where(jobs.c.id == job_id)
            row = (
                await connection.execute(
                    statement.order_by(jobs.c.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if not row:
                return None
            item = IngestionJob.model_validate(row.payload)
            claimed = item.model_copy(
                update={
                    "status": JobStatus.RUNNING,
                    "attempt": item.attempt + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            await connection.execute(
                update(jobs)
                .where(jobs.c.id == claimed.id)
                .values(
                    payload=claimed.model_dump(mode="json"), status=claimed.status.value
                )
            )
            return claimed

    async def count_chunks(self) -> int:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(chunks.c.id))).all()
        return len(rows)

    async def _insert(self, table: Table, value: Any, **extra: Any) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                table.insert().values(
                    id=value.id,
                    payload=value.model_dump(mode="json"),
                    created_at=value.created_at,
                    **extra,
                )
            )

    async def _update(self, table: Table, value: Any, **extra: Any) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                update(table)
                .where(table.c.id == value.id)
                .values(payload=value.model_dump(mode="json"), **extra)
            )

    async def _get(self, table: Table, value_id: str, model: type[T]) -> T | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(table.c.payload).where(table.c.id == value_id)
                )
            ).first()
        return model.model_validate(row.payload) if row else None  # type: ignore[attr-defined,no-any-return]

    async def _list(
        self, table: Table, model: type[T], condition: Any = None
    ) -> list[T]:
        statement = select(table.c.payload)
        if condition is not None:
            statement = statement.where(condition)
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(statement.order_by(table.c.created_at))
            ).all()
        return [model.model_validate(row.payload) for row in rows]  # type: ignore[attr-defined,no-any-return]


def _chunk_from_row(row: Any) -> KnowledgeChunk:
    payload = dict(row.payload)
    embedding = list(row.embedding) if row.embedding is not None else None
    return KnowledgeChunk.model_validate({**payload, "embedding": embedding})
