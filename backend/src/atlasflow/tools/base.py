from __future__ import annotations

from abc import ABC, abstractmethod
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


class BaseTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW
    arguments_model: ClassVar[type[BaseModel]] = EmptyArguments

    @abstractmethod
    async def run(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        raise NotImplementedError
