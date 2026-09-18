from __future__ import annotations

import asyncio
import time
from typing import Any

from atlasflow.observability import traced
from atlasflow.tools.base import BaseTool, ToolContext, ToolResult


class ToolNotFoundError(KeyError):
    pass


class ToolPermissionError(PermissionError):
    pass


class ToolRegistry:
    def __init__(self, *, timeout_seconds: float = 20, max_retries: int = 2) -> None:
        self._tools: dict[str, BaseTool] = {}
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def contains(self, name: str) -> bool:
        return name in self._tools

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk_level": tool.risk_level.value,
                "arguments_schema": tool.arguments_model.model_json_schema(),
            }
            for tool in self._tools.values()
        ]

    @traced(name="tool-registry.execute", run_type="tool")
    async def execute(
        self, name: str, raw_arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(name)
        if tool.risk_level not in context.approved_risks:
            raise ToolPermissionError(
                f"Tool '{name}' requires '{tool.risk_level.value}' approval"
            )

        arguments = tool.arguments_model.model_validate(raw_arguments)
        started = time.perf_counter()
        last_error: Exception | None = None
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                result = await asyncio.wait_for(
                    tool.run(arguments, context), timeout=self.timeout_seconds
                )
                result.duration_ms = int((time.perf_counter() - started) * 1000)
                return result
            except (TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.05 * (2**attempt))

        return ToolResult(
            success=False,
            error=str(last_error),
            duration_ms=int((time.perf_counter() - started) * 1000),
            retryable=True,
        )
