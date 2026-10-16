"""Tool ablation stays causal and cannot execute a disabled tool.

All models, tool implementations and stores in these tests are local fixtures.
They never invoke a provider, production PostgreSQL or a production Run store.
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest
from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.tool_runtime import RequiredToolError, ToolRuntime
from atlasflow.agents.workflow import ResearchWorkflow
from atlasflow.evaluation.agent.contracts import AgentExperimentPlan, AgentVariant
from atlasflow.evaluation_bindings_v2 import EvaluationToolRegistry
from atlasflow.schemas import RunEventType
from atlasflow.tools.base import BaseTool, Capability, ToolContext, ToolResult
from atlasflow.tools.registry import ToolNotFoundError, ToolRegistry
from pydantic import ValidationError

from backend.tests.fakes import FakeModelGateway, make_test_container


def variant(identifier="baseline", tools=None, **changes):
    return AgentVariant.model_validate({
        "variant_id": identifier, "name": identifier,
        "tool_allowlist": tools, **changes,
    }).model_dump(mode="json")


def tool_plan(*variants, **changes):
    return AgentExperimentPlan.model_validate({
        "comparison_focus": "tools", "variants": list(variants), **changes,
    }).model_dump(mode="json")


def scenario():
    return {
        "case_id": "local-tools", "query": "Local tool fixture only",
        "requires_knowledge": False, "expected_sources": [],
        "source_gold_status": "not_applicable", "policy_requirements": {},
        "assertions": [],
    }


class RecordingTool(BaseTool):
    def __init__(self, name, *, available=True, agents=("researcher",), planning_safe=False):
        self.name = name
        self.description = f"Local {name} fixture"
        self.capabilities = (Capability(name, f"Capability for {name}", name == "web_search"),)
        self.allowed_agents = agents
        self.planning_safe = planning_safe
        self.available = available
        self.executions = 0

    def unavailable_reason(self):
        return None if self.available else "Local fixture is not configured"

    async def run(self, arguments, context):
        self.executions += 1
        return ToolResult(success=True, data={"summary": "local result"})


class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        assert self.responses, "Unexpected fixture model request"
        return self.responses.pop(0)


def native_call(name):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "fixture-blocked-call", "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }]}


def runtime(registry, client):
    events = []

    async def emit(_run_id, event):
        events.append(event)

    return ToolRuntime(registry=registry, client=client, model="local/model", event_sink=emit), events


def test_existing_general_plan_does_not_silently_become_tool_ablation():
    plan = AgentExperimentPlan.model_validate({"variants": [variant()]})
    assert plan.comparison_focus == "general"


def test_tool_ablation_requires_two_arms():
    with pytest.raises(ValidationError, match="at least two variants"):
        tool_plan(variant(tools=[]))


@pytest.mark.parametrize("changes", [
    {"model": "different/model"},
    {"policy": {"knowledge_mode": "off", "max_tool_calls_per_turn": 2}},
    {"policy": {"knowledge_mode": "off", "planner_allow_research": True}},
])
def test_tool_ablation_rejects_model_policy_and_planner_confounders(changes):
    with pytest.raises(ValidationError, match="keep model, policy, workflow and prompt identical"):
        tool_plan(variant(tools=[]), variant("candidate", ["calculator"], **changes))


def test_tool_ablation_ignores_allowlist_order_and_arm_labels():
    with pytest.raises(ValidationError, match="different tool allowlists"):
        tool_plan(variant(tools=["calculator", "web_search"]),
                  variant("different-label", ["web_search", "calculator"]))


def test_none_means_full_catalog_and_empty_means_no_tools_in_contract():
    plan = tool_plan(variant(tools=None), variant("candidate", []))
    assert plan["variants"][0]["tool_allowlist"] is None
    assert plan["variants"][1]["tool_allowlist"] == []


def test_resolution_removes_knowledge_tool_when_knowledge_is_off():
    container = make_test_container()
    resolved = container.evaluation_v2.executor.resolve_variant(
        "agent-task-v2", variant(), {}, scenario())
    assert resolved["policy"]["knowledge_mode"] == "off"
    assert "knowledge_search" not in {row["name"] for row in resolved["tool_environment"]}
    explicit = container.evaluation_v2.executor.resolve_variant(
        "agent-task-v2", variant(tools=["knowledge_search"]), {}, scenario())
    assert explicit["tool_environment"] == []


def test_effective_comparison_rejects_full_catalog_against_same_explicit_catalog():
    container = make_test_container()
    executor = container.evaluation_v2.executor
    names = [row["name"] for row in executor.environment_identity["tool_environment"]]
    plan = tool_plan(variant(tools=None), variant("candidate", names))
    errors = executor.validate_plan("agent-task-v2", plan, [scenario()], {})
    assert any("effective available tool sets are identical" in message for message in errors)


@pytest.mark.parametrize("added_tool", ["web_search", "knowledge_search"])
def test_unavailable_or_policy_disabled_tools_do_not_create_fake_effective_difference(added_tool):
    container = make_test_container()
    context = {"environment_identity": {"tool_environment": [
        {"name": "calculator", "available": True},
        {"name": "web_search", "available": False},
        {"name": "knowledge_search", "available": True},
    ]}}
    plan = tool_plan(variant(tools=["calculator"]),
                     variant("candidate", ["calculator", added_tool]))
    errors = container.evaluation_v2.executor.validate_plan(
        "agent-task-v2", plan, [scenario()], context)
    assert any("effective available tool sets are identical" in message for message in errors)


def test_genuinely_distinct_available_tools_are_valid_intervention():
    container = make_test_container()
    plan = tool_plan(variant(tools=[]), variant("candidate", ["calculator"]))
    assert container.evaluation_v2.executor.validate_plan(
        "agent-task-v2", plan, [scenario()], {}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("genuinely_different", [False, True])
async def test_service_preflight_enforces_actual_tool_intervention(genuinely_different):
    container = make_test_container()
    service = container.evaluation_v2
    service.datasets = [{
        "dataset_id": "local-tool-fixture", "kind": "agent", "version": "fixture",
        "protocols": ["agent-task-v2"], "cases": [scenario()],
    }]
    await service.initialize()
    try:
        # Adding knowledge_search while knowledge_mode=off is not an intervention.
        candidate_tools = ["calculator"] if genuinely_different else ["knowledge_search"]
        plan = tool_plan(variant(tools=[]), variant("candidate", candidate_tools),
                         dataset_id="local-tool-fixture", mode="live", confirm_live=True)
        preflight = await service.preflight(plan, "agent")
        assert preflight["ready"] is genuinely_different
        assert any("effective available tool sets are identical" in error
                   for error in preflight["errors"]) is not genuinely_different
        assert (await container.store.list())[1] == 0
    finally:
        await service.close()
        await container.store.close()


def test_settings_catalog_explains_availability_without_exposing_credentials():
    container = make_test_container()
    settings = container.evaluation_v2.settings("agent")
    assert settings["default_comparison_focus"] == "tools"
    assert settings["tools"]
    required = {"name", "description", "available", "unavailable_reason", "capabilities",
                "allowed_agents", "planning_safe", "risk_level"}
    assert all(required <= row.keys() for row in settings["tools"])
    available = {row["name"] for row in settings["tools"] if row["available"] and row["risk_level"] == "low"}
    assert set(settings["default_tool_allowlist"]) <= available
    assert set(settings["recommended_candidate_tool_allowlist"]) <= available
    serialized = json.dumps(settings)
    assert "test-key" not in serialized
    assert "api_key" not in serialized


def test_disabled_capability_stays_visible_but_cannot_enter_function_calling_catalog():
    full = ToolRegistry()
    permitted = RecordingTool("calculator")
    blocked = RecordingTool("web_search")
    full.register(permitted)
    full.register(blocked)
    isolated = EvaluationToolRegistry(full, timeout_seconds=1, max_retries=0)
    isolated.register(permitted)
    catalog = {item["id"]: item for item in isolated.capability_catalog("researcher")}
    assert catalog["calculator"]["available"] is True
    assert catalog["web_search"]["available"] is False
    assert catalog["web_search"]["tools"] == []
    assert any("本实验组未启用" in reason for reason in catalog["web_search"]["unavailable_reasons"])
    assert {item["name"] for item in isolated.available_for("researcher")} == {"calculator"}
    assert not isolated.contains("web_search")
    assert full.contains("web_search")
    assert blocked.executions == 0


def test_disabled_capability_catalog_preserves_role_permissions():
    full = ToolRegistry()
    full.register(RecordingTool("knowledge_search", agents=("researcher", "critic")))
    full.register(RecordingTool("calculator", agents=("researcher", "planner"), planning_safe=True))
    isolated = EvaluationToolRegistry(full)
    assert {item["id"] for item in isolated.capability_catalog("planner")} == {"calculator"}
    assert {item["id"] for item in isolated.capability_catalog("critic")} == {"knowledge_search"}


@pytest.mark.asyncio
async def test_direct_registry_execution_of_disabled_tool_cannot_reach_its_implementation():
    full = ToolRegistry()
    blocked = RecordingTool("web_search")
    full.register(blocked)
    isolated = EvaluationToolRegistry(full)
    with pytest.raises(ToolNotFoundError):
        await isolated.execute("web_search", {}, ToolContext(run_id="local", agent_name="researcher"))
    assert blocked.executions == 0


@pytest.mark.asyncio
async def test_malicious_model_calling_disabled_tool_is_rejected_without_execution_or_budget():
    full = ToolRegistry()
    permitted, blocked = RecordingTool("calculator"), RecordingTool("web_search")
    full.register(permitted)
    full.register(blocked)
    isolated = EvaluationToolRegistry(full)
    isolated.register(permitted)
    client = ScriptedClient([native_call("web_search"), {"content": "done", "tool_calls": []}])
    tool_runtime, events = runtime(isolated, client)
    stage = await tool_runtime.gather(run_id="hostile-model", agent="researcher", prompt="Fixture",
                                      policy=RunPolicy(knowledge_mode="off"))
    assert stage.records == []
    assert blocked.executions == permitted.executions == 0
    assert len(events) == 1
    assert events[0].event_type == RunEventType.TOOL_FAILED
    assert events[0].data["executed"] is False
    assert events[0].data["budget_counted"] is False
    assert "TOOL_NOT_AVAILABLE" in events[0].data["error"]
    assert all({item["function"]["name"] for item in request["tools"]} == {"calculator"}
               for request in client.requests)


@pytest.mark.asyncio
async def test_required_disabled_capability_is_not_silently_removed_or_auto_enabled():
    full = ToolRegistry()
    blocked = RecordingTool("web_search")
    full.register(blocked)
    client = ScriptedClient([])
    tool_runtime, events = runtime(EvaluationToolRegistry(full), client)
    with pytest.raises(RequiredToolError, match="required capabilities are unavailable"):
        await tool_runtime.gather(run_id="missing-capability", agent="researcher", prompt="Fixture",
                                  policy=RunPolicy(knowledge_mode="off"),
                                  required_capabilities=["web_search"], requires_fresh_data=True)
    assert blocked.executions == 0
    assert client.requests == events == []


@pytest.mark.asyncio
async def test_workflow_knowledge_off_also_hides_preserved_disabled_knowledge_capability():
    full = ToolRegistry()
    full.register(RecordingTool("knowledge_search"))
    full.register(RecordingTool("calculator"))
    tool_runtime, _ = runtime(EvaluationToolRegistry(full), ScriptedClient([]))

    async def no_event(_run_id, _event):
        return None

    workflow = ResearchWorkflow(model=FakeModelGateway(), event_sink=no_event, tool_runtime=tool_runtime)
    off = workflow._capability_catalog(RunPolicy(knowledge_mode="off"))
    assert {item["id"] for item in off} == {"calculator"}
    selected = workflow._capability_catalog(RunPolicy(knowledge_mode="selected", knowledge_space="local-space"))
    assert {item["id"] for item in selected} == {"calculator", "knowledge_search"}
    assert all(item["available"] is False for item in selected)
