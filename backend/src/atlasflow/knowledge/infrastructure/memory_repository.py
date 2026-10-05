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
)


class InMemoryKnowledgeRepository:
    """Deterministic repository for tests; production uses PostgreSQL."""

    def __init__(self) -> None:
        self.spaces: dict[str, KnowledgeSpace] = {}
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
        self.spaces[space.id] = space
        return space

    async def create_document(self, document: KnowledgeDocument) -> KnowledgeDocument:
        if await self.find_document(document.space_id, document.source_id):
            raise ValueError(f"document source already exists: {document.source_id}")
        self.documents[document.id] = document
        return document

    async def update_document(self, document: KnowledgeDocument) -> KnowledgeDocument:
        self.documents[document.id] = document
        return document

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
        selected = self.generations[generation_id]
        now = datetime.now(UTC)
        for item_id, item in list(self.generations.items()):
            if item.space_id == selected.space_id and item.status == "active":
                self.generations[item_id] = item.model_copy(update={"status": "retired"})
        active = selected.model_copy(update={"status": "active", "activated_at": now})
        self.generations[generation_id] = active
        space = self.spaces[selected.space_id]
        self.spaces[space.id] = space.model_copy(
            update={"active_generation_id": generation_id, "updated_at": now}
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
        self.jobs[job.id] = job
        return job

    async def update_job(self, job: IngestionJob) -> IngestionJob:
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
