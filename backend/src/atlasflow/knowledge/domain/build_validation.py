"""Shared invariants for staging and publishing a complete knowledge space."""

from __future__ import annotations

from math import isfinite

from atlasflow.knowledge.domain.models import (
    DocumentStatus,
    DocumentVersion,
    IndexGeneration,
    IngestionJob,
    JobStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSpace,
    KnowledgeSpaceVersion,
    SpaceBuild,
    SpaceDocumentState,
    VersionStatus,
)

BUSY_JOB_STATUSES = {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED_RETRYABLE}


def ensure_no_build(space: KnowledgeSpace) -> None:
    if space.active_build_id:
        raise ValueError("space has an active build; publish or cancel it before changing the space")


def ensure_job_allowed(space: KnowledgeSpace, job: IngestionJob) -> None:
    if space.active_build_id and job.space_build_id != space.active_build_id:
        raise ValueError("space has an active build; ordinary ingestion is locked")
    if job.space_build_id and job.space_build_id != space.active_build_id:
        raise ValueError("space build is no longer active; its jobs cannot be changed")


def validate_build_baseline(
    space: KnowledgeSpace,
    base: KnowledgeSpaceVersion | None,
    build: SpaceBuild,
    current_documents: list[KnowledgeDocument],
) -> None:
    current = {
        doc.id: SpaceDocumentState(version_id=doc.current_version_id, status=doc.status)
        for doc in current_documents
    }
    if (
        base is None
        or base.space_id != space.id
        or build.space_id != space.id
        or build.base_version_id != base.id
        or space.active_version_id != base.id
        or space.active_generation_id != base.generation_id
        or current != base.documents
    ):
        raise ValueError("space build baseline changed; save the current complete space and retry")
    if set(build.documents) != set(base.documents):
        raise ValueError("space build must contain the entire baseline document manifest")
    for document_id, previous in base.documents.items():
        target = build.documents[document_id]
        if previous.status == DocumentStatus.ACTIVE:
            if target.status != DocumentStatus.ACTIVE or not target.version_id:
                raise ValueError("space build must rebuild every active document")
            if target.version_id == previous.version_id:
                raise ValueError("space build must create a new version for every active document")
        elif target != previous:
            raise ValueError("space build must preserve archived document states")


def validate_build_targets(
    build: SpaceBuild,
    generation: IndexGeneration,
    document_versions: list[DocumentVersion],
    build_jobs: list[IngestionJob],
    *,
    publishing: bool = False,
) -> None:
    if generation.id != build.generation_id or generation.space_id != build.space_id:
        raise ValueError("space build generation does not belong to the target space")
    if generation.status not in {"building", "validating"}:
        raise ValueError("space build requires an unpublished index generation")
    if build.build_request_hash:
        if generation.build_spec is None or generation.build_spec.request_hash != build.build_request_hash:
            raise ValueError("space build identity differs from its generation")
        if publishing and generation.identity_status != "verified":
            raise ValueError("space build generation identity is not verified")
    active = {
        document_id: state.version_id for document_id, state in build.documents.items()
        if state.status == DocumentStatus.ACTIVE
    }
    if not active:
        raise ValueError("space build requires at least one active document")
    versions_by_id = {version.id: version for version in document_versions}
    if len(versions_by_id) != len(active) or len(document_versions) != len(active):
        raise ValueError("space build needs exactly one new version per active document")
    jobs_by_id = {job.id: job for job in build_jobs}
    if (
        len(build.job_ids) != len(active)
        or len(set(build.job_ids)) != len(active)
        or set(jobs_by_id) != set(build.job_ids)
        or len(build_jobs) != len(active)
    ):
        raise ValueError("space build needs exactly one job per active document")
    if {(job.document_id, job.version_id) for job in build_jobs} != set(active.items()):
        raise ValueError("space build jobs do not cover the complete target manifest")
    for document_id, version_id in active.items():
        version = versions_by_id.get(version_id)
        if not version or version.document_id != document_id:
            raise ValueError("space build contains a missing or mismatched document version")
        if (version.parser_version, version.chunker_version) != (
            build.parser_version, build.chunker_version
        ):
            raise ValueError("space build document pipelines are inconsistent")
        if build.build_request_hash and version.build_request_hash != build.build_request_hash:
            raise ValueError("space build document identity is inconsistent")
        expected = VersionStatus.READY if publishing else VersionStatus.PENDING
        if version.status != expected:
            raise ValueError(f"space build document version must be {expected.value}")
    for job in build_jobs:
        if (
            job.space_id != build.space_id
            or job.space_build_id != build.id
            or job.generation_id != generation.id
        ):
            raise ValueError("space build job belongs to a different build or generation")
        if build.build_request_hash and job.build_request_hash != build.build_request_hash:
            raise ValueError("space build job identity is inconsistent")
        expected_job = JobStatus.COMPLETED if publishing else JobStatus.QUEUED
        if job.status != expected_job:
            raise ValueError(f"space build requires all jobs to be {expected_job.value}")


def validate_build_sources(
    build: SpaceBuild, base: KnowledgeSpaceVersion,
    baseline_versions: list[DocumentVersion], document_versions: list[DocumentVersion],
) -> None:
    """The bytes scheduled before the transaction must still match its locked baseline."""
    originals = {version.id: version for version in baseline_versions}
    targets = {version.id: version for version in document_versions}
    for document_id, state in base.documents.items():
        if state.status != DocumentStatus.ACTIVE:
            continue
        original = originals.get(state.version_id)
        target = targets.get(build.documents[document_id].version_id)
        if not original or not target or original.document_id != document_id:
            raise ValueError("space build source version no longer matches its baseline")
        if any(
            getattr(original, field) != getattr(target, field)
            for field in ("content_hash", "blob_key", "mime_type", "byte_size")
        ):
            raise ValueError("space build source changed before staging; retry the whole space")


def validate_build_chunks(build: SpaceBuild, indexed_chunks: list[KnowledgeChunk]) -> None:
    parents = {chunk.id: chunk for chunk in indexed_chunks if chunk.chunk_kind == "parent"}
    dimensions: set[int] = set()
    for document_id, state in build.documents.items():
        if state.status != DocumentStatus.ACTIVE:
            continue
        children = [
            chunk for chunk in indexed_chunks
            if chunk.version_id == state.version_id and chunk.chunk_kind == "child"
        ]
        if not children or any(
            chunk.document_id != document_id
            or chunk.generation_id != build.generation_id
            or not chunk.embedding
            or not all(isfinite(value) for value in chunk.embedding)
            or chunk.parent_id not in parents
            or parents[chunk.parent_id].version_id != state.version_id
            or parents[chunk.parent_id].document_id != document_id
            or parents[chunk.parent_id].generation_id != build.generation_id
            for chunk in children
        ):
            raise ValueError("space build contains an incomplete child index")
        dimensions.update(len(chunk.embedding) for chunk in children if chunk.embedding)
    if len(dimensions) != 1:
        raise ValueError("space build embedding dimensions are inconsistent")
