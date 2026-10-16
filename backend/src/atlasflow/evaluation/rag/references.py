"""Versioned, primary-source-backed answer labels, separate from immutable v1 qrels."""
from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReferenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReferenceSupport(ReferenceContract):
    source_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_number: int = Field(ge=1)
    section: str = Field(min_length=1)
    anchor: str = Field(min_length=1, max_length=400)
    fact_indices: list[int] = Field(min_length=1, max_length=12)


class ReferenceAnswer(ReferenceContract):
    case_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    answer: str = Field(min_length=1, max_length=4000)
    facts: list[str] = Field(min_length=1, max_length=12)
    supports: list[ReferenceSupport] = Field(min_length=1, max_length=20)
    verification_method: Literal["primary_source_reading"]
    curated_by: Literal["Codex"]
    verified_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def every_fact_has_a_source(self):
        if any(not fact.strip() for fact in self.facts) or len(set(self.facts)) != len(self.facts):
            raise ValueError("reference facts must be nonempty and unique")
        indices = {index for support in self.supports for index in support.fact_indices}
        if indices != set(range(len(self.facts))):
            raise ValueError("every reference fact needs a valid primary-source support")
        return self


class ReferenceAnnotations(ReferenceContract):
    schema_version: Literal["rag-references-v1"]
    base_dataset_id: str
    corpus_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_version: str = Field(min_length=1, max_length=32)
    reference_version: str = Field(min_length=1, max_length=64)
    cases: list[ReferenceAnswer]

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate answer reference case ids")
        return self


def load_reference_annotations(root: Path) -> ReferenceAnnotations:
    local = root / "evals/rag/reference-answers-v2.json"
    packaged = resources.files("atlasflow.evaluation").joinpath("data/rag-reference-answers-v2.json")
    selected = local if local.is_file() else packaged
    return ReferenceAnnotations.model_validate_json(selected.read_text(encoding="utf-8"))


def apply_reference_annotations(dataset: dict, annotations: ReferenceAnnotations, base_dataset_id: str):
    if annotations.base_dataset_id != base_dataset_id or annotations.corpus_manifest_hash != dataset["corpus_manifest_hash"]:
        raise ValueError("answer references do not match the base corpus")
    by_id = {case["case_id"]: case for case in dataset["cases"]}
    for annotation in annotations.cases:
        case = by_id.get(annotation.case_id)
        if case is None or case["query"] != annotation.query:
            raise ValueError("answer reference query/case does not match the base dataset")
        expected_sources = {source["source_id"]: source["content_hash"] for source in case["expected_sources"]}
        for support in annotation.supports:
            if (expected_sources.get(support.source_id) != support.content_hash
                    or dataset["source_content_hashes"].get(support.source_id) != support.content_hash):
                raise ValueError("answer reference primary-source identity changed")
    # Validate the whole overlay before mutating any case: a stale later label
    # must not leave earlier cases upgraded in a caller-owned dataset.
    for annotation in annotations.cases:
        case = by_id[annotation.case_id]
        case.update(reference_facts=list(annotation.facts), reference_status="verified",
                    reference_answer=annotation.answer,
                    reference_provenance=annotation.model_dump(mode="json", exclude={"answer", "facts", "query"}),
                    reference_version=annotations.reference_version)
    dataset.update(version=annotations.dataset_version, reference_version=annotations.reference_version)
    return dataset
