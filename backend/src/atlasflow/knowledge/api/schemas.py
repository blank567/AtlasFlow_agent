from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from atlasflow.knowledge.domain import KnowledgeFilter


class CreateSpaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    visibility: Literal["private", "internal", "public"] = "private"


class IngestTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=2_000_000)
    mime_type: Literal["text/plain", "text/markdown", "text/html"] = "text/plain"
    source_id: str | None = Field(default=None, max_length=300)
    canonical_uri: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    rights: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=4000)
    space: str | None = Field(default=None, max_length=100)
    filters: KnowledgeFilter | None = None
    result_limit: int = Field(default=5, ge=1, le=20)
