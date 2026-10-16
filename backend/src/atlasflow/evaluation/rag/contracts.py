"""RAG-only request contracts. No workflow policies or Agent model decisions."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atlasflow.evaluation.contracts import EvaluationBudgetSpec
from atlasflow.knowledge.domain import RetrievalTuning


class RagContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RagGenerator(RagContract):
    model: str | None = Field(default=None, min_length=1, max_length=200)
    prompt_version: Literal["rag-answer-v2", "rag-answer-v2.1"] = "rag-answer-v2.1"
    max_context_chars: int = Field(default=12_000, ge=1000, le=40_000)
    max_tokens: int = Field(default=4096, ge=128, le=4096)

    @model_validator(mode="after")
    def trusted_model_identifier(self):
        if self.model and ("://" in self.model or not re.fullmatch(r"~?[A-Za-z0-9][A-Za-z0-9_.:/-]*", self.model)):
            raise ValueError("model must be an identifier on the configured provider, not a URL")
        return self


class RagVariant(RagContract):
    variant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    retrieval_tuning: RetrievalTuning = Field(default_factory=RetrievalTuning)
    retrieval_limit: int = Field(default=5, ge=1, le=20)
    generator: RagGenerator | None = None

    @model_validator(mode="after")
    def active_retrieval_lane(self):
        if self.retrieval_tuning.lexical_weight == 0 and self.retrieval_tuning.vector_weight == 0:
            raise ValueError("both retrieval lanes cannot be disabled")
        return self


class RagExperimentPlan(RagContract):
    kind: Literal["rag"] = "rag"
    protocol: Literal["rag-retrieval-v2", "rag-answer-v2"] = "rag-retrieval-v2"
    dataset_id: str = "llm-rag-v2"
    mode: Literal["live"] = "live"
    case_ids: list[str] = Field(default_factory=list, max_length=50)
    variants: list[RagVariant] = Field(min_length=1, max_length=6)
    budget: EvaluationBudgetSpec = Field(default_factory=EvaluationBudgetSpec)
    cutoffs: list[int] = Field(default_factory=lambda: [1, 3, 5], min_length=1, max_length=4)
    space: str | None = Field(default=None, max_length=120)
    space_version_id: str | None = Field(default=None, max_length=128)
    input_refs: dict[str, str] = Field(default_factory=dict)
    repetitions: Literal[1] = 1
    allow_degraded: bool = False
    confirm_live: bool = False
    judge_enabled: bool = False
    confirm_judge: bool = False
    langsmith_sync: bool = False

    @model_validator(mode="after")
    def protocol_boundaries(self):
        # Preflight is read-only; actual execution requires confirmation in create().
        if self.input_refs:
            raise ValueError("RAG experiments always run live; frozen input_refs are not supported")
        if self.judge_enabled and not self.confirm_judge:
            raise ValueError("Judge requires a separate confirmation")
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("duplicate case ids")
        if len({v.variant_id for v in self.variants}) != len(self.variants):
            raise ValueError("duplicate variant ids")
        if len(set(self.cutoffs)) != len(self.cutoffs) or any(k not in {1, 3, 5, 10} for k in self.cutoffs):
            raise ValueError("cutoffs must be unique values from 1,3,5,10")
        if any(max(self.cutoffs) > v.retrieval_limit for v in self.variants):
            raise ValueError("requested K exceeds the captured retrieval depth")
        if self.protocol == "rag-retrieval-v2":
            if self.judge_enabled or any(v.generator is not None for v in self.variants):
                raise ValueError("retrieval-only experiments have no answer generator or answer Judge")
        else:
            self.variants = [v if v.generator is not None else v.model_copy(update={"generator": RagGenerator()})
                             for v in self.variants]
        return self


def load_rag_dataset() -> dict[str, Any]:
    """Adapt verified v1 evidence labels without changing its immutable definition."""
    from atlasflow.evaluation.dataset import DatasetRegistry
    from atlasflow.evaluation.rag.references import apply_reference_annotations, load_reference_annotations

    registry = DatasetRegistry()
    original = registry.get("llm-rag-v1")
    value = original.model_dump(mode="json")
    value.update(dataset_id="llm-rag-v2", kind="rag", version="2",
                 scoring_version="rag-evidence-v2", name="LLM知识检索与回答",
                 protocols=["rag-retrieval-v2", "rag-answer-v2"],
                 label_policy="positive_evidence_only")
    for case in value["cases"]:
        case["requires_knowledge"] = True
        case["reference_facts"] = case.get("expected_facts", [])
        case["reference_status"] = "verified" if case["reference_facts"] else "missing"
        case["qrels_status"] = "missing"
    return apply_reference_annotations(value, load_reference_annotations(registry.root), original.dataset_id)
