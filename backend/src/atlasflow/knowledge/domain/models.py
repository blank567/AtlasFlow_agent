from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DocumentStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class VersionStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class JobStage(StrEnum):
    QUEUED = "queued"
    EXTRACTING = "extracting"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    VALIDATING = "validating"
    READY = "ready"


class KnowledgeSpace(FrozenModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")  # 标识符
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    visibility: Literal["private", "internal", "public"] = "private"
    embedding_profile: str = "default-embedding-v1"
    retrieval_profile: str = "default-hybrid-v1"
    active_generation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeDocument(FrozenModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    space_id: str
    source_id: str
    title: str = Field(min_length=1, max_length=300)
    canonical_uri: str | None = None
    current_version_id: str | None = None
    status: DocumentStatus = DocumentStatus.ACTIVE
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DocumentVersion(FrozenModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    document_id: str
    version_number: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    blob_key: str
    mime_type: str
    byte_size: int = Field(ge=0)
    status: VersionStatus = VersionStatus.PENDING
    source_updated_at: datetime | None = None
    parser_version: str = "structured-parser-v1"
    chunker_version: str = "parent-child-v1"
    rights: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    indexed_at: datetime | None = None


class SourceLocator(FrozenModel):
    document_id: str
    version_id: str
    chunk_id: str | None = None
    canonical_uri: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    page_number: int | None = Field(default=None, ge=1)
    block_start: int | None = Field(default=None, ge=0)
    block_end: int | None = Field(default=None, ge=0)


class ParsedBlock(FrozenModel):
    block_type: Literal["heading", "paragraph", "list", "table", "code", "quote"]
    text: str
    heading_path: list[str] = Field(default_factory=list)
    page_number: int | None = Field(default=None, ge=1)
    order: int = Field(ge=0)
    source_locator: dict[str, Any] = Field(default_factory=dict)


class KnowledgeChunk(FrozenModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    document_id: str
    version_id: str
    generation_id: str
    parent_id: str | None = None
    chunk_kind: Literal["parent", "child"]
    title: str
    content: str
    token_count: int = Field(ge=0)
    lexical_text: str
    heading_path: list[str] = Field(default_factory=list)
    page_number: int | None = Field(default=None, ge=1)
    block_start: int | None = Field(default=None, ge=0)
    block_end: int | None = Field(default=None, ge=0)
    embedding: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class IngestionJob(FrozenModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    space_id: str
    document_id: str
    version_id: str
    status: JobStatus = JobStatus.QUEUED
    stage: JobStage = JobStage.QUEUED
    attempt: int = Field(default=0, ge=0)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class IndexGeneration(FrozenModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    space_id: str
    status: Literal["building", "validating", "active", "retired"] = "building"
    embedding_profile: str
    retrieval_profile: str
    created_at: datetime = Field(default_factory=utc_now)
    activated_at: datetime | None = None


class RetrievalProfile(FrozenModel):
    id: str = "default-hybrid-v1"
    status: Literal["uncalibrated", "calibrated"] = "uncalibrated"
    lexical_weight: float = Field(default=1.0, ge=0)
    vector_weight: float = Field(default=1.0, ge=0)
    rrf_k: int = Field(default=60, ge=1)
    candidate_limit: int = Field(default=40, ge=1, le=200)
    rerank_limit: int = Field(default=20, ge=1, le=100)
    result_limit: int = Field(default=5, ge=1, le=20)
    min_relevance: float = Field(default=0.05, ge=0, le=1)
    context_token_budget: int = Field(default=5000, ge=500, le=50000)


class RetrievalTuning(FrozenModel):
    """Request-scoped overrides for the retrieval lab; never mutates a space profile."""

    lexical_weight: float | None = Field(default=None, ge=0, le=4)
    vector_weight: float | None = Field(default=None, ge=0, le=4)
    rrf_k: int | None = Field(default=None, ge=1, le=200)
    candidate_limit: int | None = Field(default=None, ge=1, le=200)
    rerank_limit: int | None = Field(default=None, ge=1, le=100)
    rerank_enabled: bool | None = None


class FilterValue(FrozenModel):
    in_values: list[str] = Field(default_factory=list, alias="in")
    contains_any: list[str] = Field(default_factory=list)
    gte: str | None = None
    lte: str | None = None


class KnowledgeFilter(FrozenModel):
    language: FilterValue | None = None
    source_type: FilterValue | None = None
    tags: FilterValue | None = None
    authority_level: FilterValue | None = None
    published_at: FilterValue | None = None


class PrincipalContext(FrozenModel):
    subject_id: str = "local-user"
    roles: set[Literal["reader", "editor", "admin"]] = Field(
        default_factory=lambda: {"admin"}
    )
    allowed_space_ids: set[str] = Field(default_factory=set)


class RetrievalHit(FrozenModel):
    chunk: KnowledgeChunk
    lexical_score: float = 0.0
    vector_score: float = 0.0
    fusion_score: float = 0.0
    rerank_score: float | None = None


class RetrievalDecision(FrozenModel):
    status: Literal["sufficient", "partial", "insufficient"]
    reasons: list[str] = Field(default_factory=list)
    coverage: float = Field(ge=0, le=1)
    evidence_count: int = Field(ge=0)
    best_relevance: float = Field(default=0, ge=0)
    score_basis: Literal["rerank", "fusion"] = "fusion"


class RetrievalTrace(FrozenModel):
    profile_id: str
    generation_id: str | None
    lexical_candidates: int = 0
    vector_candidates: int = 0
    fused_candidates: int = 0
    rerank_candidates: int = 0
    rerank_requested: bool = True
    rerank_applied: bool = False
    applied_parameters: dict[str, float | int | bool] = Field(default_factory=dict)
    degraded: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class SearchResult(FrozenModel):
    query: str
    decision: RetrievalDecision
    hits: list[RetrievalHit] = Field(default_factory=list)
    applied_filters: KnowledgeFilter | None = None
    trace: RetrievalTrace
