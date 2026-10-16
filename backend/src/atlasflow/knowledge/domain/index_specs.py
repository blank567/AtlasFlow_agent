"""Content-addressed, credential-free index identity and self-contained read bindings."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def stable_hash(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class EmbeddingSpec(SpecModel):
    schema_version: str = "embedding-spec-v1"
    provider: str
    service_id: str
    model: str = Field(min_length=1, max_length=200)
    revision: str | None = None
    actual_model: str | None = None
    document_input_type: str = "search_document"
    query_input_type: str = "search_query"
    encoding_format: Literal["float"] = "float"
    preprocessing: str = "chunk-content-verbatim-v1"
    normalization: str = "none;cosine-at-search-v1"
    requested_dimensions: int | None = Field(default=None, ge=1)
    observed_dimensions: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def safe_identity(self) -> EmbeddingSpec:
        if ("://" in self.model or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", self.model)):
            raise ValueError("embedding model must be a model identifier, not a remote URL")
        if "://" in self.service_id or "@" in self.service_id:
            raise ValueError("service_id must be an opaque trusted service identifier")
        return self

    @property
    def request_hash(self) -> str:
        return stable_hash(self.model_dump(exclude={"observed_dimensions", "actual_model"}))

    @property
    def fingerprint(self) -> str:
        return stable_hash(self.model_dump())


class IndexBuildSpec(SpecModel):
    schema_version: str = "index-build-spec-v1"
    parser_version: str
    chunker_version: str
    child_tokens: int = Field(ge=1)
    parent_tokens: int = Field(ge=1)
    token_estimator: str = "cjk-word-symbol-v1"
    text_cleanup: str = "nul-and-line-endings-v1"
    implementation_hash: str = ""
    lexicalizer: str = "jieba-lowercase-v1"
    dependencies: dict[str, str] = Field(default_factory=dict)
    embedding: EmbeddingSpec

    @property
    def request_hash(self) -> str:
        value = self.model_dump(exclude={"embedding"})
        value["embedding_request_hash"] = self.embedding.request_hash
        return stable_hash(value)

    @property
    def fingerprint(self) -> str:
        return stable_hash(self.model_dump())


class SnapshotDocument(SpecModel):
    version_id: str | None = None
    status: Literal["active", "archived"]


class SearchSnapshot(SpecModel):
    schema_version: str = "search-snapshot-v1"
    space_id: str
    space_slug: str
    space_version_id: str | None = None
    generation_id: str | None = None
    identity_status: Literal["legacy_unknown", "declared", "verified"] = "legacy_unknown"
    build_spec: IndexBuildSpec | None = None
    documents: dict[str, SnapshotDocument] = Field(default_factory=dict)
    manifest_hash: str = ""

    @model_validator(mode="after")
    def verify_manifest(self) -> SearchSnapshot:
        expected = stable_hash(self.model_dump(exclude={"manifest_hash"}))
        if self.manifest_hash and self.manifest_hash != expected:
            raise ValueError("search snapshot manifest hash does not match its content")
        object.__setattr__(self, "manifest_hash", expected)
        if self.identity_status == "verified" and (
            self.build_spec is None or self.build_spec.embedding.observed_dimensions is None
        ):
            raise ValueError("verified snapshot requires a resolved embedding dimension")
        return self


class RetrievalSnapshotRef(SpecModel):
    snapshot: SearchSnapshot
    retrieval_parameters: dict[str, float | int | bool] = Field(default_factory=dict)
    query_pipeline_fingerprint: str = ""
    query_aliases: dict[str, Any] = Field(default_factory=dict)
