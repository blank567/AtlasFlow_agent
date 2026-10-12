from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

from atlasflow.knowledge.domain import (
    DocumentStatus,
    DocumentVersion,
    IndexGeneration,
    IngestionJob,
    JobStatus,
    KnowledgeSpaceVersion,
    SpaceBuild,
    SpaceDocumentState,
    VersionStatus,
)

if TYPE_CHECKING:
    from atlasflow.knowledge.application.platform import KnowledgePlatform


class SpaceBuildService:
    """Prepare isolated jobs and release their complete manifest in one transaction."""

    def __init__(self, platform: KnowledgePlatform) -> None:
        self.platform = platform
        self.repository = platform.repository

    async def start(self, space: str, note: str = "") -> SpaceBuild:
        selected = await self.platform.get_space(space)
        await self.platform._require_idle_space(selected.id)
        note = self.platform._validate_space_note(note)
        documents = await self.repository.list_documents(selected.id)
        active = [item for item in documents if item.status == DocumentStatus.ACTIVE]
        if not active:
            raise ValueError("space has no active documents to rebuild")
        generation = IndexGeneration(
            space_id=selected.id,
            embedding_profile=selected.embedding_profile,
            retrieval_profile=selected.retrieval_profile,
        )
        build_id = str(uuid4())
        targets = {
            item.id: SpaceDocumentState(version_id=item.current_version_id, status=item.status)
            for item in documents
        }
        original_manifest = dict(targets)
        new_versions: list[DocumentVersion] = []
        build_jobs: list[IngestionJob] = []
        # Read and validate every source before creating any job. All active
        # documents take part, including those already on the latest parser.
        for document in active:
            versions = await self.repository.list_versions(document.id)
            current = next((v for v in versions if v.id == document.current_version_id), None)
            if current is None or current.status != VersionStatus.READY:
                raise ValueError(f"document has no ready published source: {document.title}")
            source = await self.platform.blob_store.get(current.blob_key)
            if sha256(source).hexdigest() != current.content_hash:
                raise ValueError(f"published source hash mismatch: {document.title}")
            self.platform.parsers.get(current.mime_type)
            version = DocumentVersion(
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
            new_versions.append(version)
            targets[document.id] = SpaceDocumentState(
                version_id=version.id, status=DocumentStatus.ACTIVE
            )
            build_jobs.append(IngestionJob(
                space_id=selected.id,
                document_id=document.id,
                version_id=version.id,
                space_build_id=build_id,
                generation_id=generation.id,
            ))
        if not selected.active_version_id:
            baseline = await self.platform.create_space_version(
                selected.id, "整空间重建前自动保存的完整基线"
            )
            base_version_id = baseline.id
        else:
            base_version_id = selected.active_version_id
            baseline = await self.repository.get_space_version(base_version_id)
        if baseline is None or baseline.documents != original_manifest:
            raise ValueError("space changed while preparing its build; retry")
        build = SpaceBuild(
            id=build_id,
            space_id=selected.id,
            base_version_id=base_version_id,
            generation_id=generation.id,
            documents=targets,
            job_ids=[job.id for job in build_jobs],
            note=note,
        )
        # The repository rechecks baseline and competing writes under its lock.
        return await self.repository.create_space_build(build, generation, new_versions, build_jobs)

    async def get(self, space: str, build_id: str) -> SpaceBuild:
        selected = await self.platform.get_space(space)
        build = await self.repository.get_space_build(build_id)
        if build is None or build.space_id != selected.id:
            raise KeyError(f"knowledge space build not found: {build_id}")
        return await self._progress(build)

    async def list(self, space: str) -> list[SpaceBuild]:
        selected = await self.platform.get_space(space)
        builds = await self.repository.list_space_builds(selected.id)
        return [await self._progress(build) for build in builds]

    async def _progress(self, build: SpaceBuild) -> SpaceBuild:
        if build.status in {"published", "cancelled"}:
            return build
        jobs = {job.id: job for job in await self.repository.list_jobs(build.space_id)}
        selected = [jobs.get(job_id) for job_id in build.job_ids]
        if any(job is None or job.status in {
            JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL, JobStatus.CANCELLED
        } for job in selected):
            status = "failed"
        elif selected and all(job.status == JobStatus.COMPLETED for job in selected if job):
            status = "ready"
        else:
            status = "building"
        return build.model_copy(update={"status": status})

    async def publish(self, space: str, build_id: str) -> KnowledgeSpaceVersion:
        build = await self.get(space, build_id)
        if build.status not in {"ready", "published"}:
            raise ValueError("all space build jobs must complete before publishing")
        return await self.repository.publish_space_build(build.id)

    async def cancel(self, space: str, build_id: str) -> SpaceBuild:
        build = await self.get(space, build_id)
        return await self.repository.cancel_space_build(build.id)
