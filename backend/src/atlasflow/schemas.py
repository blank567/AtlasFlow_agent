from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


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
    type: str
    message: str
    agent: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class CreateRunRequest(BaseModel):
    query: str = Field(min_length=3, max_length=4000)


class IngestDocumentRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=200_000)
    source_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestDocumentResponse(BaseModel):
    document_id: str
    chunks_created: int


class RunRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    query: str
    status: RunStatus = RunStatus.PENDING
    plan: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    critiques: list[str] = Field(default_factory=list)
    report: str | None = None
    events: list[RunEvent] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
