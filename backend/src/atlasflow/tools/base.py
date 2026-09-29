from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from atlasflow.schemas import Evidence


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolContext(BaseModel):
    run_id: str
    agent_name: str
    user_query: str = ""
    approved_risks: set[RiskLevel] = Field(default_factory=lambda: {RiskLevel.LOW})


class ToolResult(BaseModel):
    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    retryable: bool = False


class EmptyArguments(BaseModel):
    pass


@dataclass(frozen=True)
class Capability:
    id: str
    description: str
    supports_fresh_data: bool = False


AGENT_ROLES = ("planner", "researcher", "critic", "synthesizer", "quality_gate")


class BaseTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW
    arguments_model: ClassVar[type[BaseModel]] = EmptyArguments
    capabilities: ClassVar[tuple[Capability, ...]] = ()
    allowed_agents: ClassVar[tuple[str, ...]] = AGENT_ROLES
    model_calls_per_execution: ClassVar[int] = 0
    planning_safe: ClassVar[bool] = False
    # Opt in only for read-only tools. Cache lifetime is one gather() invocation.
    cache_identical_calls: ClassVar[bool] = False
    transient_output: ClassVar[bool] = False
    transient_notice: ClassVar[str | None] = None
    # Some aggregate tools already perform their own deterministic recovery and
    # preflight. Re-running the whole aggregate with LLM-guessed parameters is
    # both expensive and unsafe, so they may opt into a per-stage execution cap.
    max_executions_per_stage: ClassVar[int | None] = None

    def unavailable_reason(self) -> str | None:
        """Local configuration check only; never expose credentials or probe the network."""
        return None

    @staticmethod
    def failure_scope(arguments: dict[str, Any]) -> str | None:
        """Opt in to stopping repeated terminal failures for the same target."""
        return None

    def validate_context(self, arguments: dict[str, Any], context: ToolContext) -> str | None:
        """Optional deterministic intent guard, before any execution or budget charge."""
        return None

    @staticmethod
    def safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            key: str(value)[:500] if isinstance(value, str) else value
            for key, value in arguments.items()
            if key.lower() not in {"key", "api_key", "token"}
        }

    @staticmethod
    def safe_evidence(evidence: Sequence[Evidence]) -> list[Evidence]:
        return [item.model_copy(update={"content": item.content[:1600]}) for item in evidence[:8]]

    @staticmethod
    def safe_summary(result: ToolResult) -> str | None:
        return (str(result.data.get("summary") or "")[:1000] or None) if result.success else None

    @staticmethod
    def navigation_url(result: ToolResult) -> str | None:
        return None

    @classmethod
    def navigation_urls(cls, result: ToolResult) -> list[str]:
        url = cls.navigation_url(result)
        return [url] if url else []

    @staticmethod
    def format_summary(arguments: dict[str, Any], summary: str) -> str:
        return f"{arguments} → {summary}"[:1400]

    @abstractmethod
    async def run(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        raise NotImplementedError
