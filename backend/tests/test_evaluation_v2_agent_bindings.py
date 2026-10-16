"""Exercise the real v2 Agent executor with test-only gateways, no HTTP or PG."""
from __future__ import annotations

from copy import deepcopy

import pytest
from atlasflow.agents.contracts import CritiqueRoute
from atlasflow.evaluation.agent.assertions import ordered_events
from atlasflow.evaluation.agent.contracts import AgentVariant
from atlasflow.evaluation.agent.runner import evaluate_agent, load_agent_dataset

from backend.tests.fakes import GatewayScenario, make_test_container


def case_by_id(case_id):
    return deepcopy(next(case for case in load_agent_dataset()["cases"] if case["case_id"] == case_id))


def without_knowledge(case):
    # This test deliberately changes the fixture to isolate orchestration rather
    # than pretending the real LoRA/caching source question has been researched.
    return {**case, "requires_knowledge": False, "expected_sources": [],
            "source_gold_status": "not_applicable"}


def variant(**changes):
    return AgentVariant.model_validate({"variant_id": "baseline", "name": "工程测试", **changes}).model_dump(mode="json")


@pytest.mark.asyncio
async def test_actual_approval_executor_persists_ordered_events_without_business_runs():
    container = make_test_container(scenario=GatewayScenario(initial_task_count=2, research_delay_seconds=0))
    case = without_knowledge(case_by_id("A09"))
    try:
        artifact = await container.evaluation_v2.executor("agent-contract-v2", case, variant(), {}, 1)
        events, complete = ordered_events(artifact)
        assert complete
        assert events[0]["event_type"] == "run_started"
        assert events[-1]["event_type"] in {"run_completed", "run_degraded"}
        assert artifact["content"]["auto_approve"] is False
        assert artifact["config_identity"]["policy"] == artifact["content"]["policy"]
        assert artifact["config_identity"]["policy"]["knowledge_mode"] == "off"
        assert artifact["config_identity"]["policy"]["initial_task_count"] == 2
        result = evaluate_agent(case, artifact, {"protocol": "agent-contract-v2"})
        gate = next(check for check in result["checks"] if check.get("assertion_id") == "approval-before-research")
        assert gate["value"] is True
        assert result["metrics"]["task_success"]["value"] is None
        records, total = await container.store.list()
        assert total == 0 and not records
        assert artifact["source_id_map"] == {} and artifact["snapshot"] is None
    finally:
        await container.store.close()


@pytest.mark.asyncio
async def test_actual_two_three_accept_merges_required_policy_and_retains_true_history():
    container = make_test_container(scenario=GatewayScenario(
        initial_task_count=5, research_delay_seconds=0,
        critique_routes=[CritiqueRoute.SUPPLEMENT, CritiqueRoute.ACCEPT],
    ))
    case = without_knowledge(case_by_id("A02"))
    try:
        artifact = await container.evaluation_v2.executor("agent-task-v2", case, variant(), {}, 2)
        run = artifact["content"]
        assert run["policy"]["initial_task_count"] == 2
        assert run["policy"]["required_supplement_rounds"] == 1
        assert run["policy"]["supplement_task_count"] == 1
        assert len(run["plan_lineage"]["base_plan"]["tasks"]) == 2
        assert len(run["plan_lineage"]["supplements"][0]["tasks"]) == 1
        assert len(run["plan"]["tasks"]) == 3
        assert [decision["decision"] for decision in run["critique_history"]] == ["supplement", "accept"]
        result = evaluate_agent(case, artifact, {"protocol": "agent-task-v2"})
        check = next(item for item in result["checks"] if item.get("assertion_id") == "two-three-accept")
        assert check["value"] is True
        assert result["metrics"]["trajectory_compliance"]["value"] is True
        assert result["metrics"]["task_success"]["value"] is None
        assert artifact["trial"] == 2
        assert artifact["content"]["policy"] == artifact["config_identity"]["policy"]
        assert (await container.store.list())[1] == 0
    finally:
        await container.store.close()


@pytest.mark.asyncio
async def test_agent_snapshot_maps_only_selected_space_sources():
    container = make_test_container(scenario=GatewayScenario(initial_task_count=2, research_delay_seconds=0))
    knowledge = container.knowledge
    await knowledge.start()
    try:
        await knowledge.create_space(slug="other-space", name="Other space")
        for space, source in (("user-default", "selected-source"), ("other-space", "foreign-source")):
            job = await knowledge.submit(space=space, title=source, source_id=source,
                                         content=f"# {source}\n\nAttention evidence for {source}.".encode(), mime_type="text/markdown")
            await knowledge.process_job(job.id)
        context = await container.evaluation.snapshot_resolver("user-default", None)
        case = {**case_by_id("A01"), "expected_sources": [], "source_gold_status": "not_applicable"}
        artifact = await container.evaluation_v2.executor("agent-task-v2", case, variant(), context, 1)
        assert set(artifact["source_id_map"].values()) == {"selected-source"}
        assert "foreign-source" not in artifact["source_id_map"].values()
        assert artifact["content"]["retrieval_snapshot"]["space_slug"] == "user-default"
        assert artifact["config_identity"]["policy"]["knowledge_mode"] == "selected"
        assert artifact["config_identity"]["policy"]["knowledge_space"] == "user-default"
        assert artifact["snapshot"] == artifact["content"]["retrieval_snapshot"]
        assert (await container.store.list())[1] == 0
    finally:
        await knowledge.close()
        await container.store.close()


def test_agent_executor_rejects_fake_model_switch_and_recovery_simulation():
    container = make_test_container()
    executor = container.evaluation_v2.executor
    case = without_knowledge(case_by_id("A09"))
    errors = executor.validate_case("agent-contract-v2", case, variant(model="other/model"), {})
    assert any("cannot select another" in error for error in errors)
    recovery = without_knowledge(case_by_id("A10"))
    errors = executor.validate_case("agent-contract-v2", recovery, variant(), {})
    assert any("real restarted Run" in error for error in errors)
