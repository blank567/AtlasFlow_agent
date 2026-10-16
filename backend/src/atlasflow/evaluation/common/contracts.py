from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from atlasflow.evaluation.contracts import fingerprint, utc_now


class MetricResult(BaseModel):
    """A value plus its evaluation coverage; missing is never silently zero."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    value: float | int | bool | None = None
    status: str = "not_run"
    unit: str = "ratio"
    direction: str = "higher"
    scoring_unit: str = "case"
    numerator: float | int | None = None
    denominator: float | int | None = None
    grader_id: str = "rules"
    grader_version: str = "v2"
    reason: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    variant_id: str
    trial: int = Field(default=1, ge=1, le=5)
    status: str
    input_id: str | None = None
    metrics: dict[str, MetricResult] = Field(default_factory=dict)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    stage_status: dict[str, str] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int = 0
    error: str | None = None
    degraded: list[str] = Field(default_factory=list)
    provider_calls: list[dict[str, Any]] = Field(default_factory=list)


def cell_key(case_id: str, variant_id: str, trial: int = 1) -> str:
    return f"{case_id}:{variant_id}:{trial}"


def as_dict(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def new_id() -> str:
    return str(uuid4())


def now_iso() -> str:
    return utc_now().isoformat()


def make_experiment(plan: dict, *, dataset_hash: str, context: dict) -> dict:
    return {
        "experiment_id": new_id(),
        "kind": plan["kind"],
        "protocol": plan["protocol"],
        "plan": plan,
        "dataset_hash": dataset_hash,
        "context": context,
        "status": "planned",
        "created_at": now_iso(),
        "ended_at": None,
        "results": [],
        "summary": {},
        "budget_ledger": {},
        "stop_reason": None,
        "sync_status": "not_requested",
    }


Kind = Literal["rag", "agent"]

__all__ = [
    "CaseResult", "Kind", "MetricResult", "as_dict", "cell_key", "fingerprint",
    "make_experiment", "new_id", "now_iso",
]
