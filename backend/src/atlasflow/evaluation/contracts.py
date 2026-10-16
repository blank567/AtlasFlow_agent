from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def fingerprint(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceAnchor(Contract):
    source_id: str
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_path: str
    heading: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    anchor: str = Field(min_length=5, max_length=600)


class EvaluationCase(Contract):
    case_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    kind: Literal["rag", "agent"]
    query: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list)
    answerable: bool = True
    gold_status: Literal["verified", "draft"] = "draft"
    expected_sources: list[SourceAnchor] = Field(default_factory=list)
    expected_facts: list[str] = Field(default_factory=list)
    required_invariants: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_gold(self):
        if self.answerable and self.gold_status == "verified" and not self.expected_sources:
            raise ValueError("verified answerable cases require source anchors")
        if not self.answerable and self.expected_sources:
            raise ValueError("unanswerable cases must not invent relevant sources")
        return self


class EvaluationDataset(Contract):
    dataset_id: str
    version: str
    name: str
    space: str
    corpus_manifest_hash: str
    source_content_hashes: dict[str, str]
    scoring_version: str = "source-and-anchor-v1"
    cases: list[EvaluationCase]

    @model_validator(mode="after")
    def validate_cases(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate evaluation case ids")
        for case in self.cases:
            for source in case.expected_sources:
                if self.source_content_hashes.get(source.source_id) != source.content_hash:
                    raise ValueError("source anchor hash differs from dataset manifest")
        return self


class EvaluationArm(Contract):
    arm_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    name: str
    tuning: dict[str, Any] = Field(default_factory=dict)
    agent_policy: dict[str, Any] = Field(default_factory=dict)
    result_limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def validate_tuning(self):
        from atlasflow.agents.contracts import RunPolicy
        from atlasflow.knowledge.domain import RetrievalTuning

        tuning = RetrievalTuning.model_validate(self.tuning)
        if tuning.lexical_weight == 0 and tuning.vector_weight == 0:
            raise ValueError("both retrieval lanes cannot be disabled")
        if self.agent_policy:
            RunPolicy.model_validate(self.agent_policy)
        return self


def arm_fingerprint(arm: EvaluationArm, profile: dict | None = None) -> str:
    """Hash actual parameters, not labels or sparse-vs-complete JSON formatting."""
    from atlasflow.agents.contracts import RunPolicy
    from atlasflow.knowledge.domain import RetrievalProfile, RetrievalTuning

    base = RetrievalProfile.model_validate(profile or {})
    tuning = RetrievalTuning.model_validate(arm.tuning).model_dump(exclude_none=True)
    rerank = tuning.pop("rerank_enabled", True)
    resolved = RetrievalProfile.model_validate(
        {**base.model_dump(), **tuning, "result_limit": arm.result_limit}
    )
    if resolved.lexical_weight == 0 and resolved.vector_weight == 0:
        raise ValueError("both retrieval lanes cannot be disabled")
    if rerank and resolved.rerank_limit > resolved.candidate_limit:
        raise ValueError("rerank_limit cannot exceed candidate_limit")
    if arm.result_limit > (resolved.rerank_limit if rerank else resolved.candidate_limit):
        raise ValueError("result_limit exceeds available candidates")
    return fingerprint(
        {
            "retrieval": resolved.model_dump(mode="json", exclude={"id", "status"}),
            "rerank_enabled": rerank,
            "agent_policy": RunPolicy.model_validate(arm.agent_policy).model_dump(mode="json"),
        }
    )


class EvaluationBudgetSpec(Contract):
    max_http_attempts: int = Field(default=60, ge=1, le=5000)
    max_elapsed_seconds: int = Field(default=1200, ge=1, le=7200)
    case_timeout_seconds: int = Field(default=90, ge=1, le=1200)
    concurrency: int = Field(default=2, ge=1, le=4)


class EvaluationPlan(Contract):
    dataset_id: str
    mode: Literal["replay", "live"] = "replay"
    case_ids: list[str] = Field(default_factory=list, max_length=50)
    arms: list[EvaluationArm] = Field(min_length=1, max_length=6)
    space: str | None = None
    space_version_id: str | None = None
    input_refs: dict[str, str] = Field(default_factory=dict)
    budget: EvaluationBudgetSpec = Field(default_factory=EvaluationBudgetSpec)
    confirm_live: bool = False
    judge_enabled: bool = False
    confirm_judge: bool = False
    langsmith_sync: bool = False

    @model_validator(mode="after")
    def validate_explicit_network(self):
        if self.mode == "live" and not self.confirm_live:
            raise ValueError("live evaluation requires confirm_live=true")
        if self.judge_enabled and not self.confirm_judge:
            raise ValueError("Judge requests require confirm_judge=true")
        if len({arm.arm_id for arm in self.arms}) != len(self.arms):
            raise ValueError("duplicate arm ids")
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("duplicate case ids")
        return self


class FrozenEvaluationInput(Contract):
    input_id: str = Field(default_factory=lambda: str(uuid4()))
    case_id: str
    arm_id: str
    kind: Literal["rag", "agent"]
    content: dict[str, Any]
    content_hash: str
    dataset_hash: str
    config_hash: str
    source_run_id: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    source_id_map: dict[str, str] = Field(default_factory=dict)
    source_hash_map: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    provenance: Literal["live", "imported_run", "imported_search", "fixture"] = "live"

    @model_validator(mode="after")
    def validate_content_hash(self):
        if self.content_hash != fingerprint(self.content):
            raise ValueError("frozen input content hash mismatch")
        return self


class EvaluationCaseResult(Contract):
    case_id: str
    arm_id: str
    kind: Literal["rag", "agent"]
    status: Literal["completed", "failed", "not_evaluated", "budget_stopped"]
    input_id: str | None = None
    configuration_hash: str | None = None
    input_snapshot_hash: str | None = None
    input_provenance: str | None = None
    metrics: dict[str, float | int | bool | None] = Field(default_factory=dict)
    availability: dict[str, str] = Field(default_factory=dict)
    invariant_failures: list[str] = Field(default_factory=list)
    error: str | None = None
    replayed: bool = False
    elapsed_ms: int = 0
    source_duration_ms: int | None = None
    degraded: list[str] = Field(default_factory=list)
    provider_calls: list[dict[str, Any]] = Field(default_factory=list)


class EvaluationExperiment(Contract):
    experiment_id: str = Field(default_factory=lambda: str(uuid4()))
    status: Literal["planned", "running", "completed", "partial", "failed", "cancelled"]
    plan: EvaluationPlan
    dataset_hash: str
    comparison_key: str
    snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    results: list[EvaluationCaseResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    budget_ledger: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str | None = None
    sync_status: str = "not_requested"


def default_arms() -> list[EvaluationArm]:
    return [
        EvaluationArm(
            arm_id="baseline",
            name="混合召回",
            tuning={"query_expansion_enabled": False, "rerank_enabled": False},
        ),
        EvaluationArm(
            arm_id="candidate",
            name="混合+中英扩展+重排",
            tuning={"query_expansion_enabled": True, "rerank_enabled": True},
        ),
    ]


def all_rag_arms() -> list[EvaluationArm]:
    return [
        EvaluationArm(
            arm_id="lexical",
            name="关键词",
            tuning={
                "lexical_weight": 1,
                "vector_weight": 0,
                "query_expansion_enabled": False,
                "rerank_enabled": False,
            },
        ),
        EvaluationArm(
            arm_id="semantic",
            name="语义",
            tuning={
                "lexical_weight": 0,
                "vector_weight": 1,
                "query_expansion_enabled": False,
                "rerank_enabled": False,
            },
        ),
        default_arms()[0],
        EvaluationArm(
            arm_id="reranked",
            name="混合+重排",
            tuning={"query_expansion_enabled": False, "rerank_enabled": True},
        ),
        EvaluationArm(
            arm_id="expanded",
            name="混合+扩展",
            tuning={"query_expansion_enabled": True, "rerank_enabled": False},
        ),
        default_arms()[1],
    ]
