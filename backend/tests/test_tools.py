import pytest
from atlasflow.tools import RiskLevel, ToolContext, ToolRegistry
from atlasflow.tools.builtin import (
    CalculatorTool,
    KnowledgeSearchArguments,
    WebSearchArguments,
)


@pytest.mark.asyncio
async def test_registry_executes_validated_tool() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())

    result = await registry.execute(
        "calculator",
        {"expression": "(12 + 8) / 4"},
        ToolContext(run_id="run-1", agent_name="analyst", approved_risks={RiskLevel.LOW}),
    )

    assert result.success is True
    assert result.data["result"] == 5


@pytest.mark.asyncio
async def test_calculator_rejects_code_execution() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())

    result = await registry.execute(
        "calculator",
        {"expression": "__import__('os').system('echo unsafe')"},
        ToolContext(run_id="run-1", agent_name="analyst", approved_risks={RiskLevel.LOW}),
    )

    assert result.success is False
    assert "basic arithmetic" in (result.error or "")


def test_search_tools_accept_the_api_query_limit() -> None:
    query = "研" * 4000

    assert KnowledgeSearchArguments(query=query).query == query
    assert WebSearchArguments(query=query).query == query
