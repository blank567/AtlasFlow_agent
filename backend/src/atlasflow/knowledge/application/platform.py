from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from atlasflow.knowledge.domain import (
    DocumentStatus,
    DocumentVersion,
    IndexGeneration,
    IngestionJob,
    JobStage,
    JobStatus,
    KnowledgeDocument,
    KnowledgeFilter,
    KnowledgeSpace,
    PrincipalContext,
    SearchResult,
    VersionStatus,
)
from atlasflow.knowledge.ingestion import ParserRegistry, StructuralChunker
from atlasflow.knowledge.ports import BlobStore, KnowledgeRepository
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.observability import traced
from atlasflow.providers import EmbeddingProvider


class KnowledgePlatform:
    """Application facade for spaces, versioned ingestion, jobs and retrieval."""

    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        blob_store: BlobStore,
        embedding_provider: EmbeddingProvider,
        retrieval: RetrievalService,
        default_space: str,
        worker_enabled: bool = True,
        worker_poll_seconds: float = 0.5,
    ) -> None:
        self.repository = repository
        self.blob_store = blob_store
        self.embedding_provider = embedding_provider
        self.retrieval = retrieval
        self.default_space = default_space
        self.worker_enabled = worker_enabled
        self.worker_poll_seconds = worker_poll_seconds
        self.parsers = ParserRegistry()
        self.chunker = StructuralChunker()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        await self.repository.initialize()
        await self.ensure_default_space()
        if self.worker_enabled and self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop(), name="knowledge-worker")

    async def close(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
        await self.repository.close()

    async def ensure_default_space(self) -> KnowledgeSpace:
        existing = await self.repository.get_space(self.default_space)
        if existing:
            return existing
        return await self.create_space(
            slug=self.default_space,
            name="默认知识空间",
            description="由用户显式导入内容；系统不会自动注入演示语料。",
        )

    async def create_space(
        self,
        *,
        slug: str,
        name: str,
        description: str = "",
        visibility: str = "private",
    ) -> KnowledgeSpace:
        return await self.repository.create_space(
            KnowledgeSpace(
                slug=slug,
                name=name,
                description=description,
                visibility=visibility,  # type: ignore[arg-type]
            )
        )

    async def list_spaces(self) -> list[KnowledgeSpace]:
        return await self.repository.list_spaces()

    async def get_space(self, value: str) -> KnowledgeSpace:
        space = await self.repository.get_space(value)
        if not space:
            raise KeyError(f"knowledge space not found: {value}")
        return space

    async def list_documents(self, space: str) -> list[KnowledgeDocument]:
        selected = await self.get_space(space)
        return await self.repository.list_documents(selected.id)

    async def get_document(
        self, document_id: str
    ) -> tuple[KnowledgeDocument, list[DocumentVersion]]:
        document = await self.repository.get_document(document_id)
        if not document:
            raise KeyError(f"knowledge document not found: {document_id}")
        return document, await self.repository.list_versions(document_id)

    async def submit(
        self,
        *,
        space: str,
        title: str,
        content: bytes,
        mime_type: str,
        source_id: str | None = None,
        canonical_uri: str | None = None,
        metadata: dict[str, object] | None = None,
        rights: dict[str, object] | None = None,
        provenance: dict[str, object] | None = None,
    ) -> IngestionJob:
        selected_space = await self.get_space(space)
        normalized_hash = sha256(content).hexdigest()
        source_id = source_id or str(uuid4())
        document = await self.repository.find_document(selected_space.id, source_id)
        if document is None:
            document = await self.repository.create_document(
                KnowledgeDocument(
                    space_id=selected_space.id,
                    source_id=source_id,
                    title=title,
                    canonical_uri=canonical_uri,
                    metadata=metadata or {},
                )
            )
        versions = await self.repository.list_versions(document.id)
        existing = next((item for item in versions if item.content_hash == normalized_hash), None)
        if existing:
            return await self.repository.create_job(
                IngestionJob(
                    space_id=selected_space.id,
                    document_id=document.id,
                    version_id=existing.id,
                    status=JobStatus.COMPLETED,
                    stage=JobStage.READY,
                    progress=1,
                )
            )
        version_id = str(uuid4())
        suffix = _suffix_for(mime_type)
        blob_key = f"{selected_space.id}/{document.id}/{version_id}/source{suffix}"
        await self.blob_store.put(blob_key, content)
        version = await self.repository.create_version(
            DocumentVersion(
                id=version_id,
                document_id=document.id,
                version_number=len(versions) + 1,
                content_hash=normalized_hash,
                blob_key=blob_key,
                mime_type=mime_type,
                byte_size=len(content),
                rights=rights or {},
                provenance=provenance or {},
            )
        )
        return await self.repository.create_job(
            IngestionJob(
                space_id=selected_space.id,
                document_id=document.id,
                version_id=version.id,
            )
        )

    async def process_job(self, job_id: str) -> IngestionJob:
        job = await self.repository.get_job(job_id)
        if not job:
            raise KeyError(f"knowledge job not found: {job_id}")
        if job.status == JobStatus.COMPLETED:
            return job
        if job.status != JobStatus.RUNNING:
            job = job.model_copy(
                update={
                    "status": JobStatus.RUNNING,
                    "attempt": job.attempt + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            await self.repository.update_job(job)
        try:
            return await self._process(job)
        except Exception as exc:  # noqa: BLE001 - every stage failure must persist job state
            latest = await self.repository.get_job(job.id) or job
            failed = latest.model_copy(
                update={
                    "status": JobStatus.FAILED_TERMINAL,
                    "error_code": type(exc).__name__,
                    "error_message": str(exc)[:1000],
                    "retryable": False,
                    "updated_at": datetime.now(UTC),
                }
            )
            version = await self.repository.get_version(job.version_id)
            if version:
                await self.repository.update_version(
                    version.model_copy(update={"status": VersionStatus.FAILED})
                )
            return await self.repository.update_job(failed)

    @traced(name="knowledge.ingestion.process")
    async def _process(self, job: IngestionJob) -> IngestionJob:
        version = await self.repository.get_version(job.version_id)
        document = await self.repository.get_document(job.document_id)
        space = await self.repository.get_space(job.space_id)
        if not version or not document or not space:
            raise RuntimeError("ingestion references missing knowledge records")
        job = await self._stage(job, JobStage.EXTRACTING, 0.1)
        content = await self.blob_store.get(version.blob_key)
        job = await self._stage(job, JobStage.PARSING, 0.25)
        blocks = await asyncio.to_thread(self.parsers.get(version.mime_type).parse, content)
        if not blocks:
            raise ValueError("document parser produced no usable blocks")
        generation = await self.repository.active_generation(space.id)
        if generation is None:
            generation = await self.repository.create_generation(
                IndexGeneration(
                    space_id=space.id,
                    embedding_profile=space.embedding_profile,
                    retrieval_profile=space.retrieval_profile,
                )
            )
            generation = await self.repository.activate_generation(generation.id)
        job = await self._stage(job, JobStage.CHUNKING, 0.45)
        chunks = self.chunker.chunk(
            blocks,
            document_id=document.id,
            version_id=version.id,
            generation_id=generation.id,
            title=document.title,
            metadata={**document.metadata, "canonical_uri": document.canonical_uri},
        )
        children = [item for item in chunks if item.chunk_kind == "child"]
        if not children:
            raise ValueError("document chunker produced no searchable chunks")
        job = await self._stage(job, JobStage.EMBEDDING, 0.65)
        vectors: list[list[float]] = []
        for start in range(0, len(children), 32):
            vectors.extend(
                await self.embedding_provider.embed(
                    [item.content for item in children[start : start + 32]],
                    input_type="search_document",
                )
            )
        if len(vectors) != len(children):
            raise RuntimeError("embedding provider returned an unexpected vector count")
        vector_index = {item.id: vector for item, vector in zip(children, vectors, strict=True)}
        chunks = [
            item.model_copy(update={"embedding": vector_index.get(item.id)}) for item in chunks
        ]
        job = await self._stage(job, JobStage.INDEXING, 0.82)
        await self.repository.replace_chunks(version.id, chunks)
        job = await self._stage(job, JobStage.VALIDATING, 0.95)
        now = datetime.now(UTC)
        await self.repository.update_version(
            version.model_copy(update={"status": VersionStatus.READY, "indexed_at": now})
        )
        await self.repository.update_document(
            document.model_copy(
                update={
                    "current_version_id": version.id,
                    "status": DocumentStatus.ACTIVE,
                    "updated_at": now,
                }
            )
        )
        return await self.repository.update_job(
            job.model_copy(
                update={
                    "status": JobStatus.COMPLETED,
                    "stage": JobStage.READY,
                    "progress": 1,
                    "updated_at": now,
                }
            )
        )

    async def _stage(self, job: IngestionJob, stage: JobStage, progress: float) -> IngestionJob:
        updated = job.model_copy(
            update={"stage": stage, "progress": progress, "updated_at": datetime.now(UTC)}
        )
        await self.repository.update_job(updated)
        return updated

    async def retry_job(self, job_id: str) -> IngestionJob:
        job = await self.get_job(job_id)
        if job.status not in {JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}:
            raise ValueError("only failed jobs can be retried")
        return await self.repository.update_job(
            job.model_copy(
                update={
                    "status": JobStatus.QUEUED,
                    "stage": JobStage.QUEUED,
                    "error_code": None,
                    "error_message": None,
                    "progress": 0,
                    "updated_at": datetime.now(UTC),
                }
            )
        )

    async def get_job(self, job_id: str) -> IngestionJob:
        job = await self.repository.get_job(job_id)
        if not job:
            raise KeyError(f"knowledge job not found: {job_id}")
        return job

    async def list_jobs(self, space: str | None = None) -> list[IngestionJob]:
        space_id = (await self.get_space(space)).id if space else None
        return await self.repository.list_jobs(space_id)

    async def archive_document(self, document_id: str) -> KnowledgeDocument:
        document = (await self.get_document(document_id))[0]
        return await self.repository.update_document(
            document.model_copy(
                update={"status": DocumentStatus.ARCHIVED, "updated_at": datetime.now(UTC)}
            )
        )

    async def purge_document(self, document_id: str) -> None:
        document, versions = await self.get_document(document_id)
        for version in versions:
            await self.blob_store.delete(version.blob_key)
        await self.repository.purge_document(document.id)

    async def search(
        self,
        query: str,
        *,
        space: str | None = None,
        filters: KnowledgeFilter | None = None,
        result_limit: int = 5,
        principal: PrincipalContext | None = None,
    ) -> SearchResult:
        return await self.retrieval.search(
            query,
            space=space or self.default_space,
            filters=filters,
            result_limit=result_limit,
            principal=principal,
        )

    async def stats(self) -> dict[str, int]:
        return {
            "spaces": len(await self.repository.list_spaces()),
            "chunks": await self.repository.count_chunks(),
            "jobs": len(await self.repository.list_jobs()),
        }

    async def _worker_loop(self) -> None:
        while not self._closed:
            job = await self.repository.claim_job()
            if job is None:
                await asyncio.sleep(self.worker_poll_seconds)
                continue
            await self.process_job(job.id)


def _suffix_for(mime_type: str) -> str:
    return {
        "application/pdf": ".pdf",
        "text/markdown": ".md",
        "text/x-markdown": ".md",
        "text/html": ".html",
        "application/xhtml+xml": ".html",
        "text/plain": ".txt",
    }.get(mime_type.split(";", 1)[0].lower(), Path("source.bin").suffix)
