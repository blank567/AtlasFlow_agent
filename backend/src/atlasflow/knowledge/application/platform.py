from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from atlasflow.knowledge.application.space_builds import SpaceBuildService
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
    KnowledgeSpaceVersion,
    PrincipalContext,
    RetrievalTuning,
    SearchResult,
    SpaceBuild,
    SpaceDocumentState,
    VersionStatus,
)
from atlasflow.knowledge.domain.models import CURRENT_CHUNKER_VERSION, CURRENT_PARSER_VERSION
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
        self.space_builds = SpaceBuildService(self)
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        await self.repository.initialize()
        await self.ensure_default_space()
        if self.worker_enabled and self._worker is None:
            self._worker = asyncio.create_task(
                self._worker_loop(), name="knowledge-worker"
            )

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

    async def list_space_versions(self, space: str) -> list[KnowledgeSpaceVersion]:
        selected = await self.get_space(space)
        return await self.repository.list_space_versions(selected.id)

    async def delete_space_version(
        self, space: str, version_id: str
    ) -> KnowledgeSpaceVersion:
        """Hide a historical manifest while retaining its index and audit references."""
        selected = await self.get_space(space)
        version = await self.repository.get_space_version(version_id)
        if version is None or version.space_id != selected.id:
            raise KeyError(f"knowledge space version not found: {version_id}")
        if selected.active_version_id == version.id:
            raise ValueError("current space version cannot be deleted; activate another version first")
        if version.deleted_at is None:
            await self._require_idle_space(selected.id)
        # The repository checks these conditions again under its space lock.
        return await self.repository.delete_space_version(selected.id, version.id)

    async def start_space_build(self, space: str, note: str = "") -> SpaceBuild:
        return await self.space_builds.start(space, note)

    async def list_space_builds(self, space: str) -> list[SpaceBuild]:
        return await self.space_builds.list(space)

    async def get_space_build(self, space: str, build_id: str) -> SpaceBuild:
        return await self.space_builds.get(space, build_id)

    async def publish_space_build(self, space: str, build_id: str) -> KnowledgeSpaceVersion:
        return await self.space_builds.publish(space, build_id)

    async def cancel_space_build(self, space: str, build_id: str) -> SpaceBuild:
        return await self.space_builds.cancel(space, build_id)

    async def create_space_version(self, space: str, note: str) -> KnowledgeSpaceVersion:
        """Capture one complete, settled search state for an entire knowledge space."""
        selected = await self.get_space(space)
        note = self._validate_space_note(note)
        await self._require_idle_space(selected.id)
        generation = await self.repository.active_generation(selected.id)
        documents = await self.repository.list_documents(selected.id)
        manifest: dict[str, SpaceDocumentState] = {}
        for document in documents:
            manifest[document.id] = SpaceDocumentState(
                version_id=document.current_version_id, status=document.status
            )
            if document.status == DocumentStatus.ACTIVE:
                await self._validate_snapshot_document(
                    document.id, document.current_version_id,
                    generation.id if generation else None,
                )
        versions = await self.repository.list_space_versions(selected.id)
        snapshot = KnowledgeSpaceVersion(
            space_id=selected.id,
            version_number=max((item.version_number for item in versions), default=0) + 1,
            generation_id=generation.id if generation else None,
            documents=manifest,
            note=note,
        )
        return await self.repository.create_space_version(snapshot)

    async def set_space_version_note(
        self, space: str, version_id: str, note: str
    ) -> KnowledgeSpaceVersion:
        version = await self._space_version(space, version_id)
        note = self._validate_space_note(note)
        if note == version.note:
            return version
        return await self.repository.update_space_version(version.model_copy(update={"note": note}))

    async def activate_space_version(
        self, space: str, version_id: str
    ) -> KnowledgeSpace:
        """Restore a whole-space manifest; repository applies all pointers atomically."""
        selected = await self.get_space(space)
        version = await self._space_version(space, version_id)
        await self._require_idle_space(selected.id)
        if selected.active_version_id == version.id:
            return selected
        documents = {item.id: item for item in await self.repository.list_documents(selected.id)}
        if set(version.documents) - set(documents):
            raise ValueError("space version references a removed document")
        if version.generation_id is not None:
            generation = await self.repository.get_generation(version.generation_id)
            if generation is None or generation.space_id != selected.id:
                raise ValueError("space version references a removed index generation")
        for document_id, state in version.documents.items():
            if state.version_id is not None:
                source = await self.repository.get_version(state.version_id)
                if source is None or source.document_id != document_id:
                    raise ValueError("space version references a missing document version")
            if state.status == DocumentStatus.ACTIVE:
                await self._validate_snapshot_document(
                    document_id, state.version_id, version.generation_id
                )
        return await self.repository.activate_space_version(version)

    async def _space_version(self, space: str, version_id: str) -> KnowledgeSpaceVersion:
        selected = await self.get_space(space)
        version = await self.repository.get_space_version(version_id)
        if version is None or version.space_id != selected.id or version.deleted_at is not None:
            raise KeyError(f"knowledge space version not found: {version_id}")
        return version

    @staticmethod
    def _validate_space_note(note: str) -> str:
        normalized = note.strip()
        if len(normalized) > 500:
            raise ValueError("space version note exceeds 500 characters")
        return normalized

    async def _require_idle_space(self, space_id: str) -> None:
        await self._require_mutable_space(space_id)
        jobs = await self.repository.list_jobs(space_id)
        if any(
            job.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED_RETRYABLE}
            for job in jobs
        ):
            raise ValueError("space has active ingestion jobs; wait until they finish")

    async def _require_mutable_space(self, space_id: str) -> None:
        selected = await self.get_space(space_id)
        if selected.active_build_id:
            raise ValueError("space has a staged build; publish or cancel it before changing documents")

    async def _validate_snapshot_document(
        self, document_id: str, version_id: str | None, generation_id: str | None
    ) -> None:
        version = await self.repository.get_version(version_id) if version_id else None
        if version is None or version.document_id != document_id or version.status != VersionStatus.READY:
            raise ValueError("active document has no ready version")
        if generation_id is None:
            raise ValueError("active document has no index generation")
        children = await self.repository.count_version_chunks(version.id)
        searchable = await self.repository.count_version_searchable_chunks(version.id, generation_id)
        if children == 0 or searchable != children:
            raise ValueError("active document has no complete child index in this generation")

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
        await self._require_mutable_space(selected_space.id)
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
        current = next(
            (item for item in versions if item.id == document.current_version_id), None
        )
        if (
            current
            and current.content_hash == normalized_hash
            and current.status == VersionStatus.READY
            and self._uses_current_pipeline(current)
        ):
            return await self.repository.create_job(
                IngestionJob(
                    space_id=selected_space.id,
                    document_id=document.id,
                    version_id=current.id,
                    status=JobStatus.COMPLETED,
                    stage=JobStage.READY,
                    progress=1,
                )
            )
        pending = next(
            (
                item for item in reversed(versions)
                if item.content_hash == normalized_hash
                and self._uses_current_pipeline(item)
                and item.status in {VersionStatus.PENDING, VersionStatus.PROCESSING}
            ),
            None,
        )
        if pending:
            jobs = await self.repository.list_jobs(selected_space.id)
            existing_job = next(
                (
                    job for job in jobs
                    if job.version_id == pending.id
                    and job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
                ),
                None,
            )
            if existing_job:
                return existing_job
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

    @staticmethod
    def _uses_current_pipeline(version: DocumentVersion) -> bool:
        return (
            version.parser_version == CURRENT_PARSER_VERSION
            and version.chunker_version == CURRENT_CHUNKER_VERSION
        )

    async def reindex_document(self, document_id: str) -> IngestionJob:
        """Queue a new index version from the published source without changing it in place."""
        document, versions = await self.get_document(document_id)
        await self._require_mutable_space(document.space_id)
        if document.status != DocumentStatus.ACTIVE:
            raise ValueError("archived documents cannot be reindexed")
        current = next(
            (item for item in versions if item.id == document.current_version_id), None
        )
        if current is None or current.status != VersionStatus.READY:
            raise ValueError("document has no published source version to reindex")
        if self._uses_current_pipeline(current):
            raise ValueError("document already uses the current parsing and chunking pipeline")
        pending = next(
            (
                item for item in reversed(versions)
                if item.content_hash == current.content_hash
                and self._uses_current_pipeline(item)
                and item.status in {VersionStatus.PENDING, VersionStatus.PROCESSING}
            ),
            None,
        )
        if pending:
            jobs = await self.repository.list_jobs(document.space_id)
            existing_job = next(
                (job for job in jobs if job.version_id == pending.id), None
            )
            if existing_job:
                return existing_job
        source = await self.blob_store.get(current.blob_key)
        if sha256(source).hexdigest() != current.content_hash:
            raise ValueError("published source blob does not match its stored hash")
        version = await self.repository.create_version(
            DocumentVersion(
                document_id=document.id,
                version_number=max(item.version_number for item in versions) + 1,
                content_hash=current.content_hash,
                blob_key=current.blob_key,
                mime_type=current.mime_type,
                byte_size=current.byte_size,
                source_updated_at=current.source_updated_at,
                rights=current.rights,
                provenance=current.provenance,
            )
        )
        return await self.repository.create_job(
            IngestionJob(
                space_id=document.space_id,
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
        if job.status == JobStatus.CANCELLED:
            raise ValueError("cancelled ingestion jobs cannot be processed")
        if job.space_build_id:
            await self._require_open_build_job(job)
        else:
            await self._require_mutable_space(job.space_id)
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
        except asyncio.CancelledError:
            # A normal API/worker shutdown must leave the staged job runnable.
            # Its old published space remains untouched while this job resumes.
            latest = await self.repository.get_job(job.id)
            if latest and latest.status == JobStatus.RUNNING:
                await self.repository.update_job(latest.model_copy(update={
                    "status": JobStatus.QUEUED,
                    "stage": JobStage.QUEUED,
                    "progress": 0,
                    "error_code": "WorkerInterrupted",
                    "error_message": "Worker stopped; the staged job will resume when a worker starts",
                    "retryable": True,
                    "updated_at": datetime.now(UTC),
                }))
            raise
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
        blocks = await asyncio.to_thread(
            self.parsers.get(version.mime_type).parse, content
        )
        if not blocks:
            raise ValueError("document parser produced no usable blocks")
        if job.space_build_id:
            build = await self._require_open_build_job(job)
            if (version.parser_version != CURRENT_PARSER_VERSION
                    or version.chunker_version != CURRENT_CHUNKER_VERSION):
                raise ValueError("space build pipeline differs from this worker; cancel and rebuild")
            generation = await self.repository.get_generation(build.generation_id)
            if generation is None or generation.space_id != space.id:
                raise ValueError("space build generation is missing")
        else:
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
        vector_index = {
            item.id: vector for item, vector in zip(children, vectors, strict=True)
        }
        chunks = [
            item.model_copy(update={"embedding": vector_index.get(item.id)})
            for item in chunks
        ]
        job = await self._stage(job, JobStage.INDEXING, 0.82)
        await self.repository.replace_chunks(version.id, chunks)
        job = await self._stage(job, JobStage.VALIDATING, 0.95)
        now = datetime.now(UTC)
        await self.repository.update_version(
            version.model_copy(
                update={"status": VersionStatus.READY, "indexed_at": now}
            )
        )
        if not job.space_build_id:
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

    async def _stage(
        self, job: IngestionJob, stage: JobStage, progress: float
    ) -> IngestionJob:
        updated = job.model_copy(
            update={
                "stage": stage,
                "progress": progress,
                "updated_at": datetime.now(UTC),
            }
        )
        await self.repository.update_job(updated)
        return updated

    async def retry_job(self, job_id: str) -> IngestionJob:
        job = await self.get_job(job_id)
        if job.space_build_id:
            await self._require_open_build_job(job)
        else:
            await self._require_mutable_space(job.space_id)
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

    async def _require_open_build_job(self, job: IngestionJob) -> SpaceBuild:
        build = await self.repository.get_space_build(job.space_build_id or "")
        space = await self.get_space(job.space_id)
        if (build is None or build.status in {"published", "cancelled"}
                or space.active_build_id != build.id or job.id not in build.job_ids
                or job.generation_id != build.generation_id
                or build.space_id != job.space_id
                or build.documents.get(job.document_id) != SpaceDocumentState(
                    version_id=job.version_id, status=DocumentStatus.ACTIVE
                )):
            raise ValueError("ingestion job does not belong to an active space build")
        return build

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
        await self._require_mutable_space(document.space_id)
        return await self.repository.update_document(
            document.model_copy(
                update={
                    "status": DocumentStatus.ARCHIVED,
                    "updated_at": datetime.now(UTC),
                }
            )
        )

    async def purge_document(self, document_id: str) -> None:
        document, versions = await self.get_document(document_id)
        await self._require_mutable_space(document.space_id)
        await self.repository.purge_document(document.id)
        for blob_key in {version.blob_key for version in versions}:
            await self.blob_store.delete(blob_key)

    async def search(
        self,
        query: str,
        *,
        space: str | None = None,
        filters: KnowledgeFilter | None = None,
        result_limit: int = 5,
        tuning: RetrievalTuning | None = None,
        principal: PrincipalContext | None = None,
    ) -> SearchResult:
        return await self.retrieval.search(
            query,
            space=space or self.default_space,
            filters=filters,
            result_limit=result_limit,
            tuning=tuning,
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
