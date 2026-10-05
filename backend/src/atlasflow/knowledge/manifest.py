from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ManifestSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=400)
    topic: str
    source_type: Literal["paper", "github_document", "local_file"]
    canonical_uri: str
    ingestion_policy: Literal["metadata_only", "private_index_only", "redistributable_with_notice"]
    license_spdx_id: str | None = None
    license_evidence_uri: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    repository: str | None = None
    upstream_ref: str | None = None
    resolved_commit: str | None = None
    path: str | None = None
    aliases: list[str] = Field(default_factory=list)
    language: str = "en"
    priority: Literal["P0", "P1", "P2"] = "P1"

    @model_validator(mode="after")
    def validate_source_identity(self) -> ManifestSource:
        if self.source_type == "github_document" and (
            not self.repository or not self.path or not self.upstream_ref
        ):
            raise ValueError("github_document requires repository, path and upstream_ref")
        if self.ingestion_policy == "redistributable_with_notice" and not self.license_spdx_id:
            raise ValueError("redistributable sources require a verified SPDX license")
        return self


class CorpusManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1"]
    space: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str
    description: str
    auto_download: Literal[False] = False
    sources: list[ManifestSource]

    @model_validator(mode="after")
    def unique_sources(self) -> CorpusManifest:
        values = [item.source_id for item in self.sources]
        if len(values) != len(set(values)):
            raise ValueError("manifest source_id values must be unique")
        return self


def load_manifest(path: str | Path) -> CorpusManifest:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return CorpusManifest.model_validate(value)
