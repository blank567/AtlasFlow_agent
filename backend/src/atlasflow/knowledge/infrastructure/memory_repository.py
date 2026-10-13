from __future__ import annotations

import asyncio
from datetime import UTC, datetime

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


class InMemoryKnowledgeRepository:
    """Deterministic repository for tests; production uses PostgreSQL."""

    def __init__(self) -> None:
        self.spaces: dict[str, KnowledgeSpace] = {}
        self.space_versions: dict[str, KnowledgeSpaceVersion] = {}
        self.space_builds: dict[str, SpaceBuild] = {}
        self.documents: dict[str, KnowledgeDocument] = {}
        self.versions: dict[str, DocumentVersion] = {}
        self.generations: dict[str, IndexGeneration] = {}
        self.chunks: dict[str, KnowledgeChunk] = {}
        self.jobs: dict[str, IngestionJob] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def create_space(self, space: KnowledgeSpace) -> KnowledgeSpace:
        async with self._lock:
            if any(item.slug == space.slug for item in self.spaces.values()):
                raise ValueError(f"knowledge space already exists: {space.slug}")
            self.spaces[space.id] = space
        return space

    async def list_spaces(self) -> list[KnowledgeSpace]:
        return sorted(self.spaces.values(), key=lambda item: item.created_at)

    async def get_space(self, space_id_or_slug: str) -> KnowledgeSpace | None:
        direct = self.spaces.get(space_id_or_slug)
        return direct or next(
            (item for item in self.spaces.values() if item.slug == space_id_or_slug), None
        )

    async def update_space(self, space: KnowledgeSpace) -> KnowledgeSpace:
        async with self._lock:
            existing = self.spaces[space.id]
            if existing.active_build_id or space.active_build_id:
                raise ValueError("space build pointers are managed by the build transaction")
            self.spaces[space.id] = space
        return space

    async def create_space_version(self, version: KnowledgeSpaceVersion) -> KnowledgeSpaceVersion:
        async with self._lock:
            space = self.spaces[version.space_id]
            ensure_no_build(space)
            current = {
                item.id: SpaceDocumentState(version_id=item.current_version_id, status=item.status)
                for item in self.documents.values() if item.space_id == space.id
            }
            if current != version.documents or space.active_generation_id != version.generation_id:
                raise ValueError("space changed while its version was being saved; retry")
            if any(
                job.space_id == space.id
                and job.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED_RETRYABLE}
                for job in self.jobs.values()
            ):
                raise ValueError("space has active ingestion jobs; wait until they finish")
            next_number = max(
                (item.version_number for item in self.space_versions.values() if item.space_id == space.id),
                default=0,
            ) + 1
            version = version.model_copy(update={"version_number": next_number})
            self.space_versions[version.id] = version
            self.spaces[space.id] = space.model_copy(
                update={"active_version_id": version.id, "updated_at": datetime.now(UTC)}
            )
        return version

    async def update_space_version(self, version: KnowledgeSpaceVersion) -> KnowledgeSpaceVersion:
        async with self._lock:
            latest = self.space_versions.get(version.id)
            if (
                latest is None or latest.space_id != version.space_id
                or latest.deleted_at is not None
            ):
                raise KeyError(f"knowledge space version not found: {version.id}")
            updated = latest.model_copy(update={"note": version.note})
            self.space_versions[version.id] = updated
        return updated

    async def get_space_version(self, version_id: str) -> KnowledgeSpaceVersion | None:
        return self.space_versions.get(version_id)

    async def list_space_versions(self, space_id: str) -> list[KnowledgeSpaceVersion]:
        return sorted(
            (
                item for item in self.space_versions.values()
                if item.space_id == space_id and item.deleted_at is None
            ),
            key=lambda item: item.version_number,
        )

    async def delete_space_version(
        self, space_id: str, version_id: str
    ) -> KnowledgeSpaceVersion:
        async with self._lock:
            space = self.spaces[space_id]
            version = self.space_versions.get(version_id)
            if version is None or version.space_id != space.id:
                raise KeyError(f"knowledge space version not found: {version_id}")
            if space.active_version_id == version.id:
                raise ValueError(
                    "current space version cannot be deleted; activate another version first"
                )
            if version.deleted_at is not None:
                return version
            ensure_no_build(space)
            if any(job.space_id == space.id and job.status in BUSY_JOB_STATUSES
                   for job in self.jobs.values()):
                raise ValueError("space has active ingestion jobs; wait until they finish")
            deleted = version.model_copy(update={"deleted_at": datetime.now(UTC)})
            self.space_versions[version.id] = deleted
            return deleted

    async def activate_space_version(self, version: KnowledgeSpaceVersion) -> KnowledgeSpace:
        async with self._lock:
            space = self.spaces[version.space_id]
            latest = self.space_versions.get(version.id)
            if (
                latest is None or latest.space_id != space.id
                or latest.deleted_at is not None
            ):
                raise KeyError(f"knowledge space version not found: {version.id}")
            version = latest
            ensure_no_build(space)
            if any(job.space_id == space.id and job.status in BUSY_JOB_STATUSES for job in self.jobs.values()):
                raise ValueError("space has active ingestion jobs; wait until they finish")
            space_documents = [
                item for item in self.documents.values() if item.space_id == space.id
            ]
            if set(version.documents) - {item.id for item in space_documents}:
                raise ValueError("space version references a removed document")
            now = datetime.now(UTC)
            for item in space_documents:
                state = version.documents.get(item.id)
                self.documents[item.id] = item.model_copy(
                    update={
                        "current_version_id": state.version_id if state else item.current_version_id,
                        "status": state.status if state else DocumentStatus.ARCHIVED,
                        "updated_at": now,
                    }
                )
            for item_id, item in list(self.generations.items()):
                if item.space_id != space.id:
                    continue
                if item.id == version.generation_id:
                    self.generations[item_id] = item.model_copy(
                        update={"status": "active", "activated_at": now}
                    )
                elif item.status == "active":
                    self.generations[item_id] = item.model_copy(update={"status": "retired"})
            updated = space.model_copy(
                update={
                    "active_generation_id": version.generation_id,
                    "active_version_id": version.id,
                    "updated_at": now,
                }
            )
            self.spaces[space.id] = updated
            self.space_versions[version.id] = version.model_copy(update={"activated_at": now})
            return updated

    async def create_space_build(
        self, build: SpaceBuild, generation: IndexGeneration,
        document_versions: list[DocumentVersion], build_jobs: list[IngestionJob],
    ) -> SpaceBuild:
        async with self._lock:
            space = self.spaces[build.space_id]
            ensure_no_build(space)
            validate_build_baseline(
                space, self.space_versions.get(build.base_version_id), build,
                [doc for doc in self.documents.values() if doc.space_id == space.id],
            )
            if any(job.space_id == space.id and job.status in BUSY_JOB_STATUSES for job in self.jobs.values()):
                raise ValueError("space has active ingestion jobs; wait until they finish")
            validate_build_targets(build, generation, document_versions, build_jobs)
            validate_build_sources(
                build, self.space_versions[build.base_version_id], list(self.versions.values()),
                document_versions,
            )
            if build.status != "building" or generation.id == space.active_generation_id:
                raise ValueError("space build must start with a new unpublished generation")
            if (
                build.id in self.space_builds or generation.id in self.generations
                or any(version.id in self.versions for version in document_versions)
                or any(job.id in self.jobs for job in build_jobs)
            ):
                raise ValueError("space build must use new identifiers")
            self.space_builds[build.id] = build
            self.generations[generation.id] = generation
            self.versions.update({version.id: version for version in document_versions})
            self.jobs.update({job.id: job for job in build_jobs})
            self.spaces[space.id] = space.model_copy(update={
                "active_build_id": build.id, "updated_at": datetime.now(UTC),
            })
        return build

    async def get_space_build(self, build_id: str) -> SpaceBuild | None:
        return self.space_builds.get(build_id)

    async def list_space_builds(self, space_id: str) -> list[SpaceBuild]:
        return sorted(
            (build for build in self.space_builds.values() if build.space_id == space_id),
            key=lambda build: build.created_at,
        )

    async def publish_space_build(self, build_id: str) -> KnowledgeSpaceVersion:
        async with self._lock:
            build = self.space_builds[build_id]
            if build.status == "published" and build.published_version_id:
                return self.space_versions[build.published_version_id]
            space = self.spaces[build.space_id]
            if space.active_build_id != build.id or build.status == "cancelled":
                raise ValueError("space build is not active")
            validate_build_baseline(
                space, self.space_versions.get(build.base_version_id), build,
                [doc for doc in self.documents.values() if doc.space_id == space.id],
            )
            generation = self.generations[build.generation_id]
            target_versions = [
                self.versions[state.version_id] for state in build.documents.values()
                if state.status == DocumentStatus.ACTIVE
            ]
            validate_build_targets(
                build, generation, target_versions, [self.jobs[job_id] for job_id in build.job_ids],
                publishing=True,
            )
            validate_build_chunks(build, list(self.chunks.values()))
            now = datetime.now(UTC)
            snapshot = KnowledgeSpaceVersion(
                space_id=space.id,
                version_number=max(
                    (v.version_number for v in self.space_versions.values() if v.space_id == space.id),
                    default=0,
                ) + 1,
                generation_id=generation.id, documents=build.documents, note=build.note,
                activated_at=now,
            )
            for doc_id, state in build.documents.items():
                self.documents[doc_id] = self.documents[doc_id].model_copy(update={
                    "current_version_id": state.version_id, "status": state.status, "updated_at": now,
                })
            for generation_id, item in list(self.generations.items()):
                if item.space_id == space.id and item.status == "active":
                    self.generations[generation_id] = item.model_copy(update={"status": "retired"})
            self.generations[generation.id] = generation.model_copy(update={
                "status": "active", "activated_at": now,
            })
            self.space_versions[snapshot.id] = snapshot
            self.spaces[space.id] = space.model_copy(update={
                "active_version_id": snapshot.id, "active_generation_id": generation.id,
                "active_build_id": None, "updated_at": now,
            })
            self.space_builds[build.id] = build.model_copy(update={
                "status": "published", "published_version_id": snapshot.id, "updated_at": now,
            })
            return snapshot

    async def cancel_space_build(self, build_id: str) -> SpaceBuild:
        async with self._lock:
            build = self.space_builds[build_id]
            if build.status == "cancelled":
                return build
            space = self.spaces[build.space_id]
            if space.active_build_id != build.id or build.status == "published":
                raise ValueError("space build is not active")
            build_jobs = [self.jobs[job_id] for job_id in build.job_ids]
            if any(job.status == JobStatus.RUNNING for job in build_jobs):
                raise ValueError("space build has running jobs; wait until they finish before cancelling")
            now = datetime.now(UTC)
            for job in build_jobs:
                if job.status not in {JobStatus.COMPLETED, JobStatus.CANCELLED}:
                    self.jobs[job.id] = job.model_copy(update={
                        "status": JobStatus.CANCELLED, "retryable": False, "updated_at": now,
                    })
            self.generations[build.generation_id] = self.generations[build.generation_id].model_copy(
                update={"status": "retired"}
            )
            self.spaces[space.id] = space.model_copy(update={"active_build_id": None, "updated_at": now})
            cancelled = build.model_copy(update={"status": "cancelled", "updated_at": now})
            self.space_builds[build.id] = cancelled
            return cancelled

    async def create_document(self, document: KnowledgeDocument) -> KnowledgeDocument:
        async with self._lock:
            ensure_no_build(self.spaces[document.space_id])
            if await self.find_document(document.space_id, document.source_id):
                raise ValueError(f"document source already exists: {document.source_id}")
            self.documents[document.id] = document
            await self._mark_space_unversioned(document.space_id)
        return document

    async def update_document(self, document: KnowledgeDocument) -> KnowledgeDocument:
        async with self._lock:
            ensure_no_build(self.spaces[document.space_id])
            previous = self.documents.get(document.id)
            if previous and previous.space_id != document.space_id:
                raise ValueError("a document cannot move between spaces")
            self.documents[document.id] = document
            await self._mark_space_unversioned(document.space_id)
        return document

    async def _mark_space_unversioned(self, space_id: str) -> None:
        space = self.spaces[space_id]
        if space.active_version_id is not None:
            self.spaces[space.id] = space.model_copy(
                update={"active_version_id": None, "updated_at": datetime.now(UTC)}
            )

    async def get_document(self, document_id: str) -> KnowledgeDocument | None:
        return self.documents.get(document_id)

    async def find_document(self, space_id: str, source_id: str) -> KnowledgeDocument | None:
        return next(
            (
                item
                for item in self.documents.values()
                if item.space_id == space_id and item.source_id == source_id
            ),
            None,
        )

    async def list_documents(self, space_id: str) -> list[KnowledgeDocument]:
        return sorted(
            (item for item in self.documents.values() if item.space_id == space_id),
            key=lambda item: item.created_at,
        )

    async def purge_document(self, document_id: str) -> None:
        async with self._lock:
            document = self.documents.get(document_id)
            if document:
                ensure_no_build(self.spaces[document.space_id])
            await self._purge_document(document_id)

    async def _purge_document(self, document_id: str) -> None:
        document = self.documents.get(document_id)
        version_ids = {
            item.id for item in self.versions.values() if item.document_id == document_id
        }
        self.chunks = {
            key: value for key, value in self.chunks.items() if value.document_id != document_id
        }
        self.jobs = {
            key: value for key, value in self.jobs.items() if value.document_id != document_id
        }
        self.versions = {
            key: value for key, value in self.versions.items() if key not in version_ids
        }
        self.documents.pop(document_id, None)
        if document:
            await self._mark_space_unversioned(document.space_id)

    async def create_version(self, version: DocumentVersion) -> DocumentVersion:
        self.versions[version.id] = version
        return version

    async def update_version(self, version: DocumentVersion) -> DocumentVersion:
        self.versions[version.id] = version
        return version

    async def get_version(self, version_id: str) -> DocumentVersion | None:
        return self.versions.get(version_id)

    async def list_versions(self, document_id: str) -> list[DocumentVersion]:
        return sorted(
            (item for item in self.versions.values() if item.document_id == document_id),
            key=lambda item: item.version_number,
        )

    async def create_generation(self, generation: IndexGeneration) -> IndexGeneration:
        self.generations[generation.id] = generation
        return generation

    async def get_generation(self, generation_id: str) -> IndexGeneration | None:
        return self.generations.get(generation_id)

    async def active_generation(self, space_id: str) -> IndexGeneration | None:
        space = self.spaces.get(space_id)
        return self.generations.get(space.active_generation_id) if space else None

    async def activate_generation(self, generation_id: str) -> IndexGeneration:
        async with self._lock:
            ensure_no_build(self.spaces[self.generations[generation_id].space_id])
            return await self._activate_generation(generation_id)

    async def _activate_generation(self, generation_id: str) -> IndexGeneration:
        selected = self.generations[generation_id]
        now = datetime.now(UTC)
        for item_id, item in list(self.generations.items()):
            if item.space_id == selected.space_id and item.status == "active":
                self.generations[item_id] = item.model_copy(update={"status": "retired"})
        active = selected.model_copy(update={"status": "active", "activated_at": now})
        self.generations[generation_id] = active
        space = self.spaces[selected.space_id]
        self.spaces[space.id] = space.model_copy(
            update={"active_generation_id": generation_id, "active_version_id": None, "updated_at": now}
        )
        return active

    async def replace_chunks(self, version_id: str, chunks: list[KnowledgeChunk]) -> None:
        self.chunks = {
            key: value for key, value in self.chunks.items() if value.version_id != version_id
        }
        self.chunks.update({item.id: item for item in chunks})

    async def get_chunks(self, chunk_ids: list[str]) -> list[KnowledgeChunk]:
        return [self.chunks[item] for item in chunk_ids if item in self.chunks]

    async def count_version_chunks(self, version_id: str, kind: str = "child") -> int:
        return sum(
            item.version_id == version_id and item.chunk_kind == kind
            for item in self.chunks.values()
        )

    async def count_version_searchable_chunks(
        self, version_id: str, generation_id: str
    ) -> int:
        return sum(
            item.version_id == version_id
            and item.generation_id == generation_id
            and item.chunk_kind == "child"
            and item.embedding is not None
            for item in self.chunks.values()
        )

    async def list_searchable_chunks(
        self, space_id: str, filters: KnowledgeFilter | None = None
    ) -> list[KnowledgeChunk]:
        space = self.spaces.get(space_id)
        if not space or not space.active_generation_id:
            return []
        active_documents = {
            item.id: item
            for item in self.documents.values()
            if item.space_id == space_id and item.status == DocumentStatus.ACTIVE
        }
        chunks = [
            item
            for item in self.chunks.values()
            if item.generation_id == space.active_generation_id
            and item.document_id in active_documents
            and active_documents[item.document_id].current_version_id == item.version_id
            and item.chunk_kind == "child"
        ]
        return [item for item in chunks if _matches(item, filters)]

    async def create_job(self, job: IngestionJob) -> IngestionJob:
        async with self._lock:
            ensure_job_allowed(self.spaces[job.space_id], job)
            if job.space_build_id:
                raise ValueError("space build jobs must be created in the build transaction")
            self.jobs[job.id] = job
        return job

    async def update_job(self, job: IngestionJob) -> IngestionJob:
        async with self._lock:
            latest = self.jobs[job.id]
            if (job.space_id, job.document_id, job.version_id, job.space_build_id, job.generation_id) != (
                latest.space_id, latest.document_id, latest.version_id, latest.space_build_id, latest.generation_id
            ):
                raise ValueError("ingestion job identity cannot change")
            ensure_job_allowed(self.spaces[job.space_id], job)
            self.jobs[job.id] = job
        return job

    async def get_job(self, job_id: str) -> IngestionJob | None:
        return self.jobs.get(job_id)

    async def list_jobs(self, space_id: str | None = None) -> list[IngestionJob]:
        values = self.jobs.values()
        if space_id:
            values = [item for item in values if item.space_id == space_id]
        return sorted(values, key=lambda item: item.created_at, reverse=True)

    async def claim_job(self, job_id: str | None = None) -> IngestionJob | None:
        async with self._lock:
            pending = next(
                (
                    item
                    for item in sorted(self.jobs.values(), key=lambda row: row.created_at)
                    if item.status in {JobStatus.QUEUED, JobStatus.FAILED_RETRYABLE}
                    and (job_id is None or item.id == job_id)
                    and item.space_build_id == self.spaces[item.space_id].active_build_id
                ),
                None,
            )
            if pending is None:
                return None
            claimed = pending.model_copy(
                update={
                    "status": JobStatus.RUNNING,
                    "attempt": pending.attempt + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.jobs[claimed.id] = claimed
            return claimed

    async def count_chunks(self) -> int:
        return len(self.chunks)


def _matches(chunk: KnowledgeChunk, filters: KnowledgeFilter | None) -> bool:
    if filters is None:
        return True
    metadata = chunk.metadata
    for field_name in ("language", "source_type", "authority_level", "published_at"):
        condition = getattr(filters, field_name)
        if condition is None:
            continue
        value = str(metadata.get(field_name, ""))
        if condition.in_values and value not in condition.in_values:
            return False
        if condition.gte and value < condition.gte:
            return False
        if condition.lte and value > condition.lte:
            return False
    if filters.tags:
        tags = {str(item) for item in metadata.get("tags", [])}
        if filters.tags.contains_any and tags.isdisjoint(filters.tags.contains_any):
            return False
    return True
