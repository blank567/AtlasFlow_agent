from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypeVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import ProgrammingError
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
    KnowledgeSpaceVersion,
    SpaceBuild,
    SpaceDocumentState,
)
from atlasflow.knowledge.domain.build_validation import (
    BUSY_JOB_STATUSES,
    ensure_job_allowed,
    ensure_no_build,
    validate_build_baseline,
    validate_build_chunks,
    validate_build_sources,
    validate_build_targets,
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
space_versions = Table(
    "knowledge_space_versions",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("space_id", String(36), nullable=False, index=True),
    Column("version_number", Integer, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("space_id", "version_number", name="knowledge_space_versions_number_uq"),
)
space_builds = Table(
    "knowledge_space_builds",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("space_id", String(36), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
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
                await connection.execute(select(space_versions.c.id).limit(1))
                await connection.execute(select(space_builds.c.id).limit(1))
        except ProgrammingError as exc:
            raise RuntimeError(
                "KNOWLEDGE_SCHEMA_OUTDATED: Run 'python -m alembic upgrade head' "
                "before starting the API or worker."
            ) from exc
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
        async with self.engine.begin() as connection:
            latest = await self._lock_space(connection, value.id)
            if latest.active_build_id or value.active_build_id:
                raise ValueError("space build pointers are managed by the build transaction")
            await connection.execute(update(spaces).where(spaces.c.id == value.id).values(
                payload=value.model_dump(mode="json"),
                active_generation_id=value.active_generation_id, slug=value.slug,
            ))
        return value

    async def create_space_version(self, value: KnowledgeSpaceVersion) -> KnowledgeSpaceVersion:
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    select(spaces.c.payload).where(spaces.c.id == value.space_id).with_for_update()
                )
            ).first()
            if not row:
                raise KeyError(f"knowledge space not found: {value.space_id}")
            space = KnowledgeSpace.model_validate(row.payload)
            ensure_no_build(space)
            document_rows = (
                await connection.execute(
                    select(documents.c.payload).where(documents.c.space_id == space.id)
                )
            ).all()
            current = {
                item.id: SpaceDocumentState(version_id=item.current_version_id, status=item.status)
                for item in (KnowledgeDocument.model_validate(row.payload) for row in document_rows)
            }
            if current != value.documents or space.active_generation_id != value.generation_id:
                raise ValueError("space changed while its version was being saved; retry")
            active_job = (
                await connection.execute(
                    select(jobs.c.id).where(
                        jobs.c.space_id == space.id,
                        jobs.c.status.in_(["queued", "running", "failed_retryable"]),
                    ).limit(1)
                )
            ).first()
            if active_job:
                raise ValueError("space has active ingestion jobs; wait until they finish")
            last_number = (
                await connection.execute(
                    select(space_versions.c.version_number)
                    .where(space_versions.c.space_id == space.id)
                    .order_by(space_versions.c.version_number.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            value = value.model_copy(update={"version_number": (last_number or 0) + 1})
            await connection.execute(
                space_versions.insert().values(
                    id=value.id,
                    space_id=value.space_id,
                    version_number=value.version_number,
                    payload=value.model_dump(mode="json"),
                    created_at=value.created_at,
                )
            )
            updated = space.model_copy(
                update={"active_version_id": value.id, "updated_at": datetime.now(UTC)}
            )
            await connection.execute(
                update(spaces).where(spaces.c.id == space.id).values(payload=updated.model_dump(mode="json"))
            )
        return value

    async def update_space_version(self, value: KnowledgeSpaceVersion) -> KnowledgeSpaceVersion:
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    select(space_versions.c.payload)
                    .where(space_versions.c.id == value.id)
                    .with_for_update()
                )
            ).first()
            if not row:
                raise KeyError(f"knowledge space version not found: {value.id}")
            latest = KnowledgeSpaceVersion.model_validate(row.payload)
            if latest.space_id != value.space_id or latest.deleted_at is not None:
                raise KeyError(f"knowledge space version not found: {value.id}")
            updated = latest.model_copy(update={"note": value.note})
            await connection.execute(
                update(space_versions).where(space_versions.c.id == value.id).values(
                    payload=updated.model_dump(mode="json")
                )
            )
        return updated

    async def get_space_version(self, version_id: str) -> KnowledgeSpaceVersion | None:
        return await self._get(space_versions, version_id, KnowledgeSpaceVersion)

    async def list_space_versions(self, space_id: str) -> list[KnowledgeSpaceVersion]:
        values = await self._list(
            space_versions, KnowledgeSpaceVersion, space_versions.c.space_id == space_id
        )
        return sorted(
            (value for value in values if value.deleted_at is None),
            key=lambda item: item.version_number,
        )

    async def delete_space_version(
        self, space_id: str, version_id: str
    ) -> KnowledgeSpaceVersion:
        async with self.engine.begin() as connection:
            # Lock the space before a child row, matching activation/build release.
            space = await self._lock_space(connection, space_id)
            version = await self._get_with_connection(
                connection, space_versions, version_id, KnowledgeSpaceVersion, lock=True,
            )
            if version is None or version.space_id != space.id:
                raise KeyError(f"knowledge space version not found: {version_id}")
            if space.active_version_id == version.id:
                raise ValueError(
                    "current space version cannot be deleted; activate another version first"
                )
            if version.deleted_at is not None:
                return version
            ensure_no_build(space)
            active_job = (await connection.execute(select(jobs.c.id).where(
                jobs.c.space_id == space.id,
                jobs.c.status.in_([status.value for status in BUSY_JOB_STATUSES]),
            ).limit(1))).first()
            if active_job:
                raise ValueError("space has active ingestion jobs; wait until they finish")
            deleted = version.model_copy(update={"deleted_at": datetime.now(UTC)})
            await connection.execute(update(space_versions).where(
                space_versions.c.id == version.id,
            ).values(payload=deleted.model_dump(mode="json")))
            return deleted

    async def activate_space_version(self, value: KnowledgeSpaceVersion) -> KnowledgeSpace:
        """Restore every document pointer and the generation in one transaction."""
        now = datetime.now(UTC)
        async with self.engine.begin() as connection:
            # Every publisher/worker takes the space lock before any child row.
            space = await self._lock_space(connection, value.space_id)
            ensure_no_build(space)
            version_row = (
                await connection.execute(
                    select(space_versions.c.payload)
                    .where(space_versions.c.id == value.id)
                    .with_for_update()
                )
            ).first()
            if not version_row:
                raise KeyError(f"knowledge space version not found: {value.id}")
            value = KnowledgeSpaceVersion.model_validate(version_row.payload)
            if value.space_id != space.id or value.deleted_at is not None:
                raise KeyError(f"knowledge space version not found: {value.id}")
            doc_rows = (
                await connection.execute(
                    select(documents.c.payload)
                    .where(documents.c.space_id == space.id)
                    .with_for_update()
                )
            ).all()
            current = {
                document.id: document
                for document in (KnowledgeDocument.model_validate(row.payload) for row in doc_rows)
            }
            if set(value.documents) - set(current):
                raise ValueError("space version references a removed document")
            active_job = (
                await connection.execute(
                    select(jobs.c.id).where(
                        jobs.c.space_id == space.id,
                        jobs.c.status.in_(["queued", "running", "failed_retryable"]),
                    ).limit(1)
                )
            ).first()
            if active_job:
                raise ValueError("space has active ingestion jobs; wait until they finish")
            for document_id, state in value.documents.items():
                if state.status != DocumentStatus.ACTIVE:
                    continue
                version_row = (
                    await connection.execute(
                        select(versions.c.payload).where(versions.c.id == state.version_id)
                    )
                ).first()
                version = DocumentVersion.model_validate(version_row.payload) if version_row else None
                if not version or version.document_id != document_id or version.status != "ready":
                    raise ValueError("space version contains a missing or unready document version")
                child_rows = (
                    await connection.execute(
                        select(chunks.c.generation_id, chunks.c.embedding).where(
                            chunks.c.version_id == version.id, chunks.c.chunk_kind == "child"
                        )
                    )
                ).all()
                if not child_rows or any(
                    row.generation_id != value.generation_id or row.embedding is None
                    for row in child_rows
                ):
                    raise ValueError("space version contains an incomplete child index")
            for document in current.values():
                state = value.documents.get(document.id)
                restored = document.model_copy(
                    update={
                        "current_version_id": state.version_id if state else document.current_version_id,
                        "status": state.status if state else DocumentStatus.ARCHIVED,
                        "updated_at": now,
                    }
                )
                await connection.execute(
                    update(documents).where(documents.c.id == document.id).values(
                        payload=restored.model_dump(mode="json"),
                        current_version_id=restored.current_version_id,
                        status=restored.status.value,
                    )
                )
            if value.generation_id is not None:
                generation_row = (
                    await connection.execute(
                        select(generations.c.payload).where(
                            generations.c.id == value.generation_id,
                            generations.c.space_id == space.id,
                        )
                    )
                ).first()
                if not generation_row:
                    raise ValueError("space version references a removed index generation")
            generation_rows = (
                await connection.execute(
                    select(generations.c.payload).where(generations.c.space_id == space.id)
                )
            ).all()
            for row in generation_rows:
                generation = IndexGeneration.model_validate(row.payload)
                if generation.id == value.generation_id:
                    changed = generation.model_copy(update={"status": "active", "activated_at": now})
                elif generation.status == "active":
                    changed = generation.model_copy(update={"status": "retired"})
                else:
                    continue
                await connection.execute(
                    update(generations).where(generations.c.id == changed.id).values(
                        payload=changed.model_dump(mode="json"), status=changed.status
                    )
                )
            updated_space = space.model_copy(update={
                "active_generation_id": value.generation_id,
                "active_version_id": value.id,
                "updated_at": now,
            })
            await connection.execute(
                update(spaces).where(spaces.c.id == space.id).values(
                    payload=updated_space.model_dump(mode="json"),
                    active_generation_id=value.generation_id,
                )
            )
            activated = value.model_copy(update={"activated_at": now})
            await connection.execute(
                update(space_versions).where(space_versions.c.id == value.id).values(
                    payload=activated.model_dump(mode="json")
                )
            )
        return updated_space

    async def create_space_build(
        self, build: SpaceBuild, generation: IndexGeneration,
        document_versions: list[DocumentVersion], build_jobs: list[IngestionJob],
    ) -> SpaceBuild:
        async with self.engine.begin() as connection:
            space = await self._lock_space(connection, build.space_id)
            ensure_no_build(space)
            base = await self._get_with_connection(
                connection, space_versions, build.base_version_id, KnowledgeSpaceVersion,
            )
            current = await self._space_documents(connection, space.id)
            validate_build_baseline(space, base, build, current)
            active_job = (await connection.execute(select(jobs.c.id).where(
                jobs.c.space_id == space.id,
                jobs.c.status.in_(["queued", "running", "failed_retryable"]),
            ).limit(1))).first()
            if active_job:
                raise ValueError("space has active ingestion jobs; wait until they finish")
            validate_build_targets(build, generation, document_versions, build_jobs)
            baseline_ids = [
                state.version_id for state in base.documents.values()
                if state.status == DocumentStatus.ACTIVE
            ]
            rows = (await connection.execute(select(versions.c.payload).where(
                versions.c.id.in_(baseline_ids)
            ))).all()
            validate_build_sources(
                build, base, [DocumentVersion.model_validate(row.payload) for row in rows],
                document_versions,
            )
            if build.status != "building" or generation.id == space.active_generation_id:
                raise ValueError("space build must start with a new unpublished generation")
            await self._insert_with_connection(
                connection, generations, generation, space_id=space.id, status=generation.status,
            )
            for version in document_versions:
                await self._insert_with_connection(
                    connection, versions, version, document_id=version.document_id,
                    content_hash=version.content_hash,
                )
            for job in build_jobs:
                await self._insert_with_connection(
                    connection, jobs, job, space_id=space.id, document_id=job.document_id,
                    status=job.status.value,
                )
            await self._insert_with_connection(
                connection, space_builds, build, space_id=space.id, status=build.status,
            )
            updated = space.model_copy(update={
                "active_build_id": build.id, "updated_at": datetime.now(UTC),
            })
            await connection.execute(update(spaces).where(spaces.c.id == space.id).values(
                payload=updated.model_dump(mode="json"),
            ))
        return build

    async def get_space_build(self, build_id: str) -> SpaceBuild | None:
        return await self._get(space_builds, build_id, SpaceBuild)

    async def list_space_builds(self, space_id: str) -> list[SpaceBuild]:
        return await self._list(space_builds, SpaceBuild, space_builds.c.space_id == space_id)

    async def publish_space_build(self, build_id: str) -> KnowledgeSpaceVersion:
        async with self.engine.begin() as connection:
            space, build = await self._lock_build(connection, build_id)
            if build.status == "published" and build.published_version_id:
                snapshot = await self._get_with_connection(
                    connection, space_versions, build.published_version_id, KnowledgeSpaceVersion,
                )
                if snapshot is None:
                    raise ValueError("published space build snapshot is missing")
                return snapshot
            if space.active_build_id != build.id or build.status == "cancelled":
                raise ValueError("space build is not active")
            base = await self._get_with_connection(
                connection, space_versions, build.base_version_id, KnowledgeSpaceVersion,
            )
            current = await self._space_documents(connection, space.id)
            validate_build_baseline(space, base, build, current)
            generation = await self._get_with_connection(
                connection, generations, build.generation_id, IndexGeneration,
            )
            if generation is None:
                raise ValueError("space build index generation is missing")
            target_ids = [
                state.version_id for state in build.documents.values()
                if state.status == DocumentStatus.ACTIVE
            ]
            version_rows = (await connection.execute(select(versions.c.payload).where(
                versions.c.id.in_(target_ids)
            ))).all()
            job_rows = (await connection.execute(select(jobs.c.payload).where(
                jobs.c.id.in_(build.job_ids)
            ).order_by(jobs.c.id).with_for_update())).all()
            validate_build_targets(
                build, generation,
                [DocumentVersion.model_validate(row.payload) for row in version_rows],
                [IngestionJob.model_validate(row.payload) for row in job_rows], publishing=True,
            )
            chunk_rows = (await connection.execute(select(chunks.c.payload, chunks.c.embedding).where(
                chunks.c.version_id.in_(target_ids),
            ))).all()
            validate_build_chunks(build, [_chunk_from_row(row) for row in chunk_rows])
            now = datetime.now(UTC)
            last_number = (await connection.execute(select(space_versions.c.version_number).where(
                space_versions.c.space_id == space.id,
            ).order_by(space_versions.c.version_number.desc()).limit(1))).scalar_one_or_none()
            snapshot = KnowledgeSpaceVersion(
                space_id=space.id, version_number=(last_number or 0) + 1,
                generation_id=generation.id, documents=build.documents, note=build.note,
                activated_at=now,
            )
            await self._insert_with_connection(
                connection, space_versions, snapshot, space_id=space.id,
                version_number=snapshot.version_number,
            )
            for document in current:
                state = build.documents[document.id]
                restored = document.model_copy(update={
                    "current_version_id": state.version_id, "status": state.status, "updated_at": now,
                })
                await connection.execute(update(documents).where(documents.c.id == document.id).values(
                    payload=restored.model_dump(mode="json"), current_version_id=state.version_id,
                    status=state.status.value,
                ))
            await self._switch_generation(connection, space.id, generation.id, now)
            updated = space.model_copy(update={
                "active_generation_id": generation.id, "active_version_id": snapshot.id,
                "active_build_id": None, "updated_at": now,
            })
            await connection.execute(update(spaces).where(spaces.c.id == space.id).values(
                payload=updated.model_dump(mode="json"), active_generation_id=generation.id,
            ))
            published = build.model_copy(update={
                "status": "published", "published_version_id": snapshot.id, "updated_at": now,
            })
            await connection.execute(update(space_builds).where(space_builds.c.id == build.id).values(
                payload=published.model_dump(mode="json"), status=published.status,
            ))
            return snapshot

    async def cancel_space_build(self, build_id: str) -> SpaceBuild:
        async with self.engine.begin() as connection:
            space, build = await self._lock_build(connection, build_id)
            if build.status == "cancelled":
                return build
            if space.active_build_id != build.id or build.status == "published":
                raise ValueError("space build is not active")
            job_rows = (await connection.execute(select(jobs.c.payload).where(
                jobs.c.id.in_(build.job_ids)
            ).order_by(jobs.c.id).with_for_update())).all()
            build_jobs = [IngestionJob.model_validate(row.payload) for row in job_rows]
            if any(job.status == JobStatus.RUNNING for job in build_jobs):
                raise ValueError("space build has running jobs; wait until they finish before cancelling")
            now = datetime.now(UTC)
            for job in build_jobs:
                if job.status in {JobStatus.COMPLETED, JobStatus.CANCELLED}:
                    continue
                cancelled_job = job.model_copy(update={
                    "status": JobStatus.CANCELLED, "retryable": False, "updated_at": now,
                })
                await connection.execute(update(jobs).where(jobs.c.id == job.id).values(
                    payload=cancelled_job.model_dump(mode="json"), status=cancelled_job.status.value,
                ))
            generation = await self._get_with_connection(
                connection, generations, build.generation_id, IndexGeneration,
            )
            if generation:
                retired = generation.model_copy(update={"status": "retired"})
                await connection.execute(update(generations).where(generations.c.id == generation.id).values(
                    payload=retired.model_dump(mode="json"), status=retired.status,
                ))
            updated = space.model_copy(update={"active_build_id": None, "updated_at": now})
            await connection.execute(update(spaces).where(spaces.c.id == space.id).values(
                payload=updated.model_dump(mode="json"),
            ))
            cancelled = build.model_copy(update={"status": "cancelled", "updated_at": now})
            await connection.execute(update(space_builds).where(space_builds.c.id == build.id).values(
                payload=cancelled.model_dump(mode="json"), status=cancelled.status,
            ))
            return cancelled

    async def create_document(self, value: KnowledgeDocument) -> KnowledgeDocument:
        if await self.find_document(value.space_id, value.source_id):
            raise ValueError(f"document source already exists: {value.source_id}")
        async with self.engine.begin() as connection:
            await self._clear_active_space_version(connection, value.space_id)
            await connection.execute(documents.insert().values(
                id=value.id,
                space_id=value.space_id,
                source_id=value.source_id,
                current_version_id=value.current_version_id,
                status=value.status.value,
                payload=value.model_dump(mode="json"),
                created_at=value.created_at,
            ))
        return value

    async def update_document(self, value: KnowledgeDocument) -> KnowledgeDocument:
        async with self.engine.begin() as connection:
            await self._clear_active_space_version(connection, value.space_id)
            await connection.execute(
                update(documents).where(documents.c.id == value.id).values(
                    payload=value.model_dump(mode="json"),
                    status=value.status.value,
                    current_version_id=value.current_version_id,
                )
            )
        return value

    async def _clear_active_space_version(self, connection: Any, space_id: str) -> None:
        row = (
            await connection.execute(
                select(spaces.c.payload).where(spaces.c.id == space_id).with_for_update()
            )
        ).first()
        if row:
            space = KnowledgeSpace.model_validate(row.payload)
            ensure_no_build(space)
            if space.active_version_id:
                changed = space.model_copy(update={
                    "active_version_id": None, "updated_at": datetime.now(UTC)
                })
                await connection.execute(
                    update(spaces).where(spaces.c.id == space_id).values(
                        payload=changed.model_dump(mode="json")
                    )
                )

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
            row = (
                await connection.execute(
                    select(documents.c.space_id).where(documents.c.id == document_id)
                )
            ).first()
            if row:
                await self._clear_active_space_version(connection, row.space_id)
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
        active = selected.model_copy(update={"status": "active", "activated_at": now})
        async with self.engine.begin() as connection:
            space = await self._lock_space(connection, selected.space_id)
            ensure_no_build(space)
            await self._switch_generation(connection, space.id, selected.id, now)
            changed = space.model_copy(update={
                "active_generation_id": active.id, "active_version_id": None, "updated_at": now,
            })
            await connection.execute(update(spaces).where(spaces.c.id == space.id).values(
                payload=changed.model_dump(mode="json"), active_generation_id=active.id,
            ))
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

    async def count_version_searchable_chunks(
        self, version_id: str, generation_id: str
    ) -> int:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(chunks.c.id).where(
                        chunks.c.version_id == version_id,
                        chunks.c.generation_id == generation_id,
                        chunks.c.chunk_kind == "child",
                        chunks.c.embedding.is_not(None),
                    )
                )
            ).all()
        return len(rows)

    async def list_searchable_chunks(
        self, space_id: str, filters: KnowledgeFilter | None = None
    ) -> list[KnowledgeChunk]:
        statement = (
            select(chunks.c.payload, chunks.c.embedding)
            .select_from(chunks.join(documents, chunks.c.document_id == documents.c.id)
                         .join(spaces, documents.c.space_id == spaces.c.id))
            .where(
                spaces.c.id == space_id,
                chunks.c.generation_id == spaces.c.active_generation_id,
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
        async with self.engine.begin() as connection:
            space = await self._lock_space(connection, value.space_id)
            ensure_job_allowed(space, value)
            if value.space_build_id:
                raise ValueError("space build jobs must be created in the build transaction")
            await self._insert_with_connection(
                connection, jobs, value, space_id=value.space_id,
                document_id=value.document_id, status=value.status.value,
            )
        return value

    async def update_job(self, value: IngestionJob) -> IngestionJob:
        async with self.engine.begin() as connection:
            space = await self._lock_space(connection, value.space_id)
            latest = await self._get_with_connection(connection, jobs, value.id, IngestionJob, lock=True)
            if latest is None:
                raise KeyError(f"knowledge job not found: {value.id}")
            if (value.space_id, value.document_id, value.version_id, value.space_build_id, value.generation_id) != (
                latest.space_id, latest.document_id, latest.version_id, latest.space_build_id, latest.generation_id
            ):
                raise ValueError("ingestion job identity cannot change")
            ensure_job_allowed(space, value)
            if latest.status in {JobStatus.COMPLETED, JobStatus.CANCELLED} and value.status != latest.status:
                raise ValueError("terminal ingestion job cannot be restarted")
            await connection.execute(update(jobs).where(jobs.c.id == value.id).values(
                payload=value.model_dump(mode="json"), status=value.status.value,
            ))
        return value

    async def get_job(self, job_id: str) -> IngestionJob | None:
        return await self._get(jobs, job_id, IngestionJob)

    async def list_jobs(self, space_id: str | None = None) -> list[IngestionJob]:
        condition = jobs.c.space_id == space_id if space_id else None
        values = await self._list(jobs, IngestionJob, condition)
        return sorted(values, key=lambda item: item.created_at, reverse=True)

    async def claim_job(self, job_id: str | None = None) -> IngestionJob | None:
        # Find a candidate without a row lock, then lock its space before the
        # job. Publish/cancel use the same order, so a claim cannot deadlock or
        # resurrect a job cancelled while this worker was selecting it.
        statement = select(jobs.c.payload).where(jobs.c.status.in_(["queued", "failed_retryable"]))
        if job_id is not None:
            statement = statement.where(jobs.c.id == job_id)
        async with self.engine.connect() as connection:
            candidate = (await connection.execute(statement.order_by(jobs.c.created_at).limit(1))).first()
        if not candidate:
            return None
        initial = IngestionJob.model_validate(candidate.payload)
        async with self.engine.begin() as connection:
            space = await self._lock_space(connection, initial.space_id)
            item = await self._get_with_connection(connection, jobs, initial.id, IngestionJob, lock=True)
            if not item or item.status not in {JobStatus.QUEUED, JobStatus.FAILED_RETRYABLE}:
                return None
            if item.space_build_id != space.active_build_id:
                return None
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

    async def _lock_space(self, connection: Any, space_id: str) -> KnowledgeSpace:
        value = await self._get_with_connection(connection, spaces, space_id, KnowledgeSpace, lock=True)
        if value is None:
            raise KeyError(f"knowledge space not found: {space_id}")
        return value

    async def _lock_build(self, connection: Any, build_id: str) -> tuple[KnowledgeSpace, SpaceBuild]:
        initial = await self._get_with_connection(connection, space_builds, build_id, SpaceBuild)
        if initial is None:
            raise KeyError(f"knowledge space build not found: {build_id}")
        space = await self._lock_space(connection, initial.space_id)
        build = await self._get_with_connection(connection, space_builds, build_id, SpaceBuild, lock=True)
        if build is None:
            raise KeyError(f"knowledge space build not found: {build_id}")
        return space, build

    async def _space_documents(self, connection: Any, space_id: str) -> list[KnowledgeDocument]:
        rows = (await connection.execute(select(documents.c.payload).where(
            documents.c.space_id == space_id,
        ))).all()
        return [KnowledgeDocument.model_validate(row.payload) for row in rows]

    async def _get_with_connection(
        self, connection: Any, table: Table, value_id: str, model: type[T], *, lock: bool = False
    ) -> T | None:
        statement = select(table.c.payload).where(table.c.id == value_id)
        if lock:
            statement = statement.with_for_update()
        row = (await connection.execute(statement)).first()
        return model.model_validate(row.payload) if row else None

    async def _insert_with_connection(
        self, connection: Any, table: Table, value: Any, **extra: Any
    ) -> None:
        await connection.execute(table.insert().values(
            id=value.id, payload=value.model_dump(mode="json"), created_at=value.created_at, **extra,
        ))

    async def _switch_generation(
        self, connection: Any, space_id: str, generation_id: str, now: datetime
    ) -> None:
        rows = (await connection.execute(select(generations.c.payload).where(
            generations.c.space_id == space_id,
        ))).all()
        for row in rows:
            item = IndexGeneration.model_validate(row.payload)
            if item.id == generation_id:
                changed = item.model_copy(update={"status": "active", "activated_at": now})
            elif item.status == "active":
                changed = item.model_copy(update={"status": "retired"})
            else:
                continue
            await connection.execute(update(generations).where(generations.c.id == item.id).values(
                payload=changed.model_dump(mode="json"), status=changed.status,
            ))

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
