from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


@dataclass
class RunBundle:
    control: dict[str, Any]
    invocations: dict[str, dict[str, Any]] = field(default_factory=dict)
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    outbox: dict[str, dict[str, Any]] = field(default_factory=dict)


class ExecutionRepository(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def bind(self, run_id: str, control: dict[str, Any]) -> None: ...
    async def read(self, run_id: str) -> RunBundle | None: ...
    async def run_ids(self) -> list[str]: ...
    async def mutate(self, run_id: str, operation: Callable[[RunBundle], T]) -> T: ...
