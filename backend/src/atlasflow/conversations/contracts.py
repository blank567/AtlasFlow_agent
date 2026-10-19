"""Conversation memory is application data, not an ever-growing graph checkpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atlasflow.agents.contracts import ResearchPlan, ResearchResult, RunPolicy
from atlasflow.schemas import Evidence, RunRecord, ToolCallRecord, utc_now

Action = Literal["clarify", "answer", "research", "revise"]
TurnStatus = Literal[
    "routing",
    "awaiting_clarification",
    "awaiting_confirmation",
    "running",
    "waiting_approval",
    "interrupted",
    "completed",
    "completed_with_warnings",
    "failed",
    "cancelled",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class RequirementPatch(Contract):
    key: str = Field(min_length=1, max_length=64)
    value: str | None = Field(max_length=400)
    source_message_ids: list[str] = Field(min_length=1, max_length=8)


class Requirement(Contract):
    value: str
    source_message_ids: list[str]
    confirmed: bool = False


class ConversationDecision(Contract):
    action: Action
    resolved_request: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=400)
    base_run_id: str | None = None
    clarification_questions: list[str] = Field(default_factory=list, max_length=4)
    requirement_updates: list[RequirementPatch] = Field(default_factory=list, max_length=20)
    reset_requirements: bool = False

    @model_validator(mode="after")
    def executable_route(self):
        if self.action == "clarify" and not any(q.strip() for q in self.clarification_questions):
            raise ValueError("clarify requires a concrete question")
        if self.action != "clarify" and self.clarification_questions:
            raise ValueError("only clarify may contain clarification questions")
        if self.action in {"answer", "revise"} and not self.base_run_id:
            raise ValueError("answer/revise requires an existing report")
        keys = [item.key for item in self.requirement_updates]
        if len(keys) != len(set(keys)):
            raise ValueError("requirement keys must be unique")
        return self


class ConversationAnswer(Contract):
    content: str = Field(min_length=1, max_length=6000)


class CreateConversation(Contract):
    title: str = Field(default="新会话", min_length=1, max_length=100)
    policy: RunPolicy = Field(default_factory=lambda: RunPolicy(knowledge_mode="off"))
    auto_approve: bool = False


class SendMessage(Contract):
    content: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=0)
    base_run_id: str | None = None


class ConfirmTurn(Contract):
    expected_version: int = Field(ge=0)
    confirmation_turn_id: str


class DeleteConversation(Contract):
    confirmation_conversation_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=0)


class ConversationDeleted(Contract):
    conversation_id: str
    deleted_run_count: int = Field(ge=0)


class Conversation(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = "新会话"
    version: int = 0
    content_version: int = 0
    policy: RunPolicy
    auto_approve: bool = False
    requirements: dict[str, Requirement] = Field(default_factory=dict)
    current_report_run_id: str | None = None
    active_turn_id: str | None = None
    # Persist deletion intent before stopping work. A failed cleanup is safely
    # retryable and cannot accept more messages or dispatch another Run.
    deleting: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def restore_content_version(self):
        # Older records did not distinguish trace-only projection updates.
        # An after-validator preserves JSON-mode strict datetime parsing.
        if "content_version" not in self.model_fields_set:
            self.content_version = self.version
        return self


class Message(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    conversation_id: str
    turn_id: str
    sequence: int = 0
    role: Literal["user", "assistant"]
    kind: Literal["text", "clarification", "proposal", "report", "error", "notice"] = "text"
    content: str
    run_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Turn(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    conversation_id: str
    user_message_id: str
    request_id: str
    request_hash: str
    status: TurnStatus = "routing"
    action: Action | None = None
    requested_base_run_id: str | None = None
    decision: ConversationDecision | None = None
    requirements_snapshot: dict[str, Requirement] = Field(default_factory=dict)
    run_id: str | None = None
    error: str | None = None
    trace_segments: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ReportLink(Contract):
    run_id: str
    turn_id: str
    title: str
    status: str
    source_run_id: str | None
    created_at: datetime


class ConversationDetail(Contract):
    conversation: Conversation
    messages: list[Message]
    turns: list[Turn]
    reports: list[ReportLink] = Field(default_factory=list)
    active_run: RunRecord | None = None


class ContinuationInput(Contract):
    """Persisted server-owned seed; the HTTP client cannot supply this payload."""

    conversation_id: str
    turn_id: str
    mode: Literal["research", "report_revision"]
    base_run_id: str | None = None
    base_query: str = ""
    base_report: str = ""
    base_citation_source_urls: list[str] = Field(default_factory=list)
    base_plan: ResearchPlan | None = None
    base_results: list[ResearchResult] = Field(default_factory=list)
    base_evidence: list[Evidence] = Field(default_factory=list)
    base_tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    base_warnings: list[str] = Field(default_factory=list)


class ConversationNotFoundError(KeyError):
    pass


class ConversationConflict(ValueError):
    pass
