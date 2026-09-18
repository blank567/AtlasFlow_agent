import pytest
from atlasflow.schemas import RunStatus

from backend.tests.fakes import make_test_container


@pytest.mark.asyncio
async def test_workflow_completes_with_tools_evidence_and_report() -> None:
    container = make_test_container()

    run = await container.run_service.execute_and_wait(
        "AtlasFlow 的 RAG 与工具调用是怎样设计的？"
    )

    assert run.status == RunStatus.COMPLETED
    assert {call.tool_name for call in run.tool_calls} == {"knowledge_search", "web_search"}
    assert run.evidence
    assert run.report is not None
    assert "[S1]" in run.report
    assert any(event.agent == "critic" for event in run.events)
