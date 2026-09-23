from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atlasflow.agents.contracts import (
    AgentError,
    CritiqueDecision,
    DraftVersion,
    ExecutionMetrics,
    PlanLineage,
    QualityDecision,
    ResearchPlan,
    ResearchResult,
    ReviewContext,
    RouteRecord,
    RunPolicy,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def terminal(cls) -> frozenset[RunStatus]:
        return frozenset(
            {
                cls.COMPLETED,
                cls.COMPLETED_WITH_WARNINGS,
                cls.FAILED,
                cls.CANCELLED,
            }
        )


class ApprovalAction(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    CANCEL = "cancel"


class RunEventType(StrEnum):
    RUN_STARTED = "run_started"
    NODE_STARTED = "node_started"
    NODE_SUCCEEDED = "node_succeeded"
    NODE_FAILED = "node_failed"
    PLAN_CREATED = "plan_created"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    TASK_SCHEDULED = "task_scheduled"
    TASK_COMPLETED = "task_completed"
    REVIEW_DECIDED = "review_decided"
    ROUTE_SELECTED = "route_selected"
    QUALITY_EVALUATED = "quality_evaluated"
    RUN_COMPLETED = "run_completed"
    RUN_DEGRADED = "run_degraded"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


class Evidence(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source_id: str
    title: str
    content: str
    uri: str | None = None
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCallRecord(BaseModel):
    tool_name: str
    arguments: dict[str, Any]
    success: bool
    duration_ms: int
    evidence_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class RunEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    sequence: int = Field(default=0, ge=0)
    run_id: str
    event_type: RunEventType
    message: str
    agent: str | None = None
    node: str | None = None
    task_id: str | None = None
    plan_version: int | None = Field(default=None, ge=1)
    attempt: int | None = Field(default=None, ge=1)
    status: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    decision_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    query: str = Field(min_length=3, max_length=4000)
    auto_approve: bool = True
    policy: RunPolicy = Field(default_factory=RunPolicy)


class ApprovalRequest(BaseModel):
    action: ApprovalAction
    edited_plan: ResearchPlan | None = None

    @model_validator(mode="after")
    def validate_edited_plan(self) -> ApprovalRequest:
        if self.action is ApprovalAction.EDIT and self.edited_plan is None:
            raise ValueError("edited_plan is required when action='edit'")
        if self.action is not ApprovalAction.EDIT and self.edited_plan is not None:
            raise ValueError("edited_plan is only allowed when action='edit'")
        return self


class IngestDocumentRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=200_000)
    source_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestDocumentResponse(BaseModel):
    document_id: str
    chunks_created: int


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    query: str
    auto_approve: bool = True
    policy: RunPolicy = Field(default_factory=RunPolicy)
    status: RunStatus = RunStatus.PENDING
    plan: ResearchPlan | None = None
    plans: list[ResearchPlan] = Field(default_factory=list)
    plan_lineage: PlanLineage | None = None
    plan_lineages: list[PlanLineage] = Field(default_factory=list)
    research_results: list[ResearchResult] = Field(default_factory=list)
    critique_history: list[CritiqueDecision] = Field(default_factory=list)
    review_contexts: list[ReviewContext] = Field(default_factory=list)
    draft_versions: list[DraftVersion] = Field(default_factory=list)
    quality_history: list[QualityDecision] = Field(default_factory=list)
    route_history: list[RouteRecord] = Field(default_factory=list)
    errors: list[AgentError] = Field(default_factory=list)
    metrics: ExecutionMetrics = Field(default_factory=ExecutionMetrics)
    report: str | None = None
    warnings: list[str] = Field(default_factory=list)
    events: list[RunEvent] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_terminal_invariants(self) -> RunRecord:
        if self.status in {
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_WARNINGS,
        } and (not self.report or not self.report.strip()):
            raise ValueError(f"{self.status.value} runs require a non-empty report")
        if self.status is RunStatus.COMPLETED_WITH_WARNINGS and not self.warnings:
            raise ValueError(
                "completed_with_warnings runs require at least one warning"
            )
        if self.status is RunStatus.FAILED and not self.error:
            raise ValueError("failed runs require an error")
        return self
