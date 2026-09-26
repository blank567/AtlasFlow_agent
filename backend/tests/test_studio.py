from __future__ import annotations

import pytest
from atlasflow.config import Settings
from atlasflow.studio import build_studio_graph

from backend.tests.fakes import FakeModelGateway, GatewayScenario


def _studio_graph():
    settings = Settings(_env_file=None, langsmith_tracing=False, langsmith_api_key="")
    return build_studio_graph(
        settings=settings,
        model=FakeModelGateway(GatewayScenario(research_delay_seconds=0)),
    )


def test_studio_exposes_the_real_workflow_and_simple_input_schema() -> None:
    graph = _studio_graph()
    nodes = set(graph.get_graph().nodes)
    assert graph.checkpointer is None  # Agent Server owns Studio thread checkpoints.
    assert {"studio_entry", "supervisor", "planner", "researcher", "critic", "quality_gate", "finalizer"} <= nodes
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert {("critic", "planner"), ("critic", "schedule_wave"), ("quality_gate", "synthesizer")} <= edges
    properties = graph.get_input_jsonschema()["properties"]
    assert set(properties) == {"query", "auto_approve", "policy"}


@pytest.mark.asyncio
async def test_studio_run_initializes_state_and_finishes_without_fastapi_store() -> None:
    graph = _studio_graph()
    result = await graph.ainvoke(
        {"query": "验证 Studio 执行路径", "policy": {"initial_task_count": 2}},
        config={"recursion_limit": 96},
    )
    assert result["final_status"] == "completed"
    assert result["policy"].initial_task_count == 2
    assert len(result["plan"].tasks) == 2
    assert result["route_history"]
    assert result["route_history"][0].from_node == "supervisor"


@pytest.mark.asyncio
async def test_studio_rejects_empty_query_before_provider_call() -> None:
    graph = _studio_graph()
    with pytest.raises(ValueError, match="non-empty query"):
        await graph.ainvoke({"query": "  "})
    with pytest.raises(TypeError, match="auto_approve must be a boolean"):
        await graph.ainvoke({"query": "test", "auto_approve": "false"})
