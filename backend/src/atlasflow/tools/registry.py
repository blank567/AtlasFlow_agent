from __future__ import annotations

import asyncio
import re
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
        if len({cap.id for cap in tool.capabilities}) != len(tool.capabilities):
            raise ValueError("duplicate tool capabilities")
        for capability in tool.capabilities:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", capability.id):
                raise ValueError(f"invalid capability id: {capability.id}")
            for existing in self._tools.values():
                for previous in existing.capabilities:
                    if previous.id == capability.id and previous != capability:
                        raise ValueError(f"conflicting capability definition: {capability.id}")
        self._tools[tool.name] = tool

    def contains(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise ToolNotFoundError(name)
        return self._tools[name]

    def available_for(self, agent: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.describe()
            if agent in item["allowed_agents"] and item["available"] and item["risk_level"] == "low"
            and (agent != "planner" or self.get(item["name"]).planning_safe)
        ]

    def capability_catalog(self, agent: str = "researcher") -> list[dict[str, Any]]:
        """One capability may have several implementations; unavailable intent stays visible."""
        catalog: dict[str, dict[str, Any]] = {}
        for tool in self._tools.values():
            if agent not in tool.allowed_agents or (agent == "planner" and not tool.planning_safe):
                continue
            reason = tool.unavailable_reason()
            if tool.risk_level.value != "low":
                reason = reason or "需要额外风险授权，当前 Agent 工具阶段不可用"
            for capability in tool.capabilities:
                item = catalog.setdefault(
                    capability.id,
                    {
                        "id": capability.id,
                        "description": capability.description,
                        "supports_fresh_data": capability.supports_fresh_data,
                        "available": False,
                        "tools": [],
                        "unavailable_reasons": [],
                    },
                )
                if reason:
                    item["unavailable_reasons"].append(f"{tool.name}: {reason}")
                else:
                    item["available"] = True
                    item["tools"].append(tool.name)
        return list(catalog.values())

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk_level": tool.risk_level.value,
                "arguments_schema": tool.arguments_model.model_json_schema(),
                "capabilities": [cap.id for cap in tool.capabilities],
                "allowed_agents": list(tool.allowed_agents),
                "planning_safe": tool.planning_safe,
                "available": tool.unavailable_reason() is None,
                "unavailable_reason": tool.unavailable_reason(),
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
            raise ToolPermissionError(f"Tool '{name}' requires '{tool.risk_level.value}' approval")
        if context.agent_name == "planner" and not tool.planning_safe:
            raise ToolPermissionError("Planner only permits planning-safe reconnaissance tools")
        reason = tool.unavailable_reason()
        if reason:
            return ToolResult(success=False, error=reason)

        arguments = tool.arguments_model.model_validate(raw_arguments)
        constraint_error = tool.validate_context(raw_arguments, context)
        if constraint_error:
            return ToolResult(success=False, error=constraint_error)
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
