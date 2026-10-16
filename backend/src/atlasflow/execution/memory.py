"""Injectable deterministic test adapter. Never a production fallback."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from typing import Any, TypeVar

from atlasflow.execution.contracts import ExecutionConflict
from atlasflow.execution.ports import RunBundle

T = TypeVar("T")


class MemoryExecutionRepository:
    def __init__(self) -> None:
        self.runs: dict[str, RunBundle] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def bind(self, run_id: str, control: dict[str, Any]) -> None:
        async with self._lock:
            self.runs.setdefault(run_id, RunBundle(deepcopy(control)))

    async def read(self, run_id: str) -> RunBundle | None:
        async with self._lock:
            return deepcopy(self.runs.get(run_id))

    async def run_ids(self) -> list[str]:
        async with self._lock:
            return list(self.runs)

    async def mutate(self, run_id: str, operation: Callable[[RunBundle], T]) -> T:
        async with self._lock:
            if run_id not in self.runs:
                raise ExecutionConflict("execution binding not found")
            staged = deepcopy(self.runs[run_id])
            result = operation(staged)
            self.runs[run_id] = staged
            return deepcopy(result)
