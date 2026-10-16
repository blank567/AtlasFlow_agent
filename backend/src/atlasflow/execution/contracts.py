"""Versioned, content-safe execution persistence contracts."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ExecutionControlError(RuntimeError):
    """Never turn persistence/claim failures into model or tool retries."""


class PersistenceUnavailable(ExecutionControlError):
    pass


class ExecutionConflict(ExecutionControlError):
    pass


class UnknownInvocation(ExecutionControlError):
    pass


class ExecutionCancelled(ExecutionControlError):
    pass


class UnknownCall(BaseModel):
    call_id: str
    kind: str
    name: str
    tool_budget_counted: bool = False
    usage: dict[str, Any] | None = None


class RecoveryInfo(BaseModel):
    available: bool = False
    reason: str | None = None
    checkpoint_id: str | None = None
    recovery_count: int = 0
    unknown_calls: list[UnknownCall] = Field(default_factory=list)
    blocked_by: str | None = None
    provider_usage: dict[str, Any] = Field(default_factory=dict)


class ResumeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    expected_checkpoint_id: str = Field(min_length=1, max_length=256)
    acknowledged_unknown_call_ids: list[str] = Field(default_factory=list, max_length=200)


class ExecutionClaim(BaseModel):
    run_id: str
    owner_token: str = Field(default_factory=lambda: str(uuid4()))
    fencing_epoch: int


class InvocationLease(BaseModel):
    run_id: str
    invocation_id: str
    slot: str
    claim: ExecutionClaim
    replay: Any = None
    replayed: bool = False


class HttpAttemptToken(BaseModel):
    lease: InvocationLease


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                         default=lambda item: item.model_dump(mode="json")
                         if isinstance(item, BaseModel) else str(item))
    return hashlib.sha256(encoded.encode()).hexdigest()


def encode_safe(value: Any) -> Any:
    """Explicit JSON type tags; no pickle or arbitrary module loading."""
    from atlasflow.observability import redact_sensitive_data
    if isinstance(value, BaseModel):
        name = value.__class__.__name__
        if name not in checkpoint_types():
            raise ValueError(f"unregistered durable type: {name}")
        return {"$type": name, "data": redact_sensitive_data(value.model_dump(mode="json"))}
    if isinstance(value, datetime):
        return {"$type": "datetime", "data": value.isoformat()}
    if isinstance(value, Enum):
        return {"$type": f"enum:{value.__class__.__name__}", "data": value.value}
    if isinstance(value, dict):
        return {str(key): encode_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_safe(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_data(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise ValueError(f"unsupported durable value: {type(value).__name__}")


def decode_safe(value: Any) -> Any:
    if isinstance(value, list):
        return [decode_safe(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$type", "data"}:
            if value["$type"] == "datetime":
                return datetime.fromisoformat(value["data"])
            if value["$type"].startswith("enum:"):
                from atlasflow.agents.contracts import (
                    CritiqueRoute,
                    QualityRoute,
                    ReplanReason,
                    Severity,
                )
                from atlasflow.schemas import RunStatus
                enums = {enum.__name__: enum for enum in
                         (RunStatus, CritiqueRoute, QualityRoute, ReplanReason, Severity)}
                enum = enums.get(value["$type"].removeprefix("enum:"))
                if enum is None:
                    raise ExecutionConflict("unregistered durable enum")
                return enum(value["data"])
            model = checkpoint_types().get(value["$type"])
            if model is None:
                raise ExecutionConflict("unsupported durable result schema")
            return model.model_validate(value["data"])
        return {key: decode_safe(item) for key, item in value.items()}
    return value


def checkpoint_types() -> dict[str, type[BaseModel]]:
    from atlasflow.agents import contracts
    from atlasflow.schemas import Evidence, ToolCallRecord
    names = ("AgentError", "CritiqueDecision", "DraftVersion", "ExecutionMetrics",
             "PlanLineage", "QualityDecision", "ResearchPlan", "ResearchResult",
             "ResearchTask", "ReviewContext", "ReviewIssue", "RouteRecord", "RunPolicy",
             "SupplementBatch")
    return {**{name: getattr(contracts, name) for name in names},
            "Evidence": Evidence, "ToolCallRecord": ToolCallRecord,
            "RecoveryInfo": RecoveryInfo, "UnknownCall": UnknownCall}
