"""Versioned Agent scenarios; deliberately contains no RAG tuning parameters."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from atlasflow.agents.contracts import RunPolicy
from atlasflow.evaluation.contracts import Contract, EvaluationBudgetSpec, SourceAnchor

ASSERTION_KINDS = {
    "initial_task_count", "supplement_sequence", "dependency_order",
    "approval_before_research", "tool_budget", "replan_requires_critical_issue",
    "required_capabilities", "tool_allowlist", "nonretryable_failure_not_repeated",
    "recovery_evidence",
}


class AgentAssertion(Contract):
    assertion_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    kind: Literal[
        "initial_task_count", "supplement_sequence", "dependency_order",
        "approval_before_research", "tool_budget", "replan_requires_critical_issue",
        "required_capabilities", "tool_allowlist", "nonretryable_failure_not_repeated",
        "recovery_evidence",
    ]
    parameters: dict[str, Any] = Field(default_factory=dict)
    required: bool = True

    @model_validator(mode="after")
    def validate_parameters(self):
        specifications = {
            "initial_task_count": {"count": int},
            "supplement_sequence": {"initial_count": int, "added_count": int},
            "required_capabilities": {"capabilities": list},
            "tool_allowlist": {"tools": list},
        }
        allowed = {
            **{key: set(value) for key, value in specifications.items()},
            "dependency_order": {"require_dependencies"},
            "tool_budget": {"max_calls"},
            "approval_before_research": set(),
            "replan_requires_critical_issue": set(),
            "nonretryable_failure_not_repeated": set(),
            "recovery_evidence": set(),
        }
        if self.parameters.keys() - allowed[self.kind]:
            raise ValueError(f"unknown {self.kind} assertion parameters")
        for name, expected_type in specifications.get(self.kind, {}).items():
            value = self.parameters.get(name)
            if type(value) is not expected_type:
                raise ValueError(f"{self.kind} requires {name}:{expected_type.__name__}")
            if expected_type is int and value < 1:
                raise ValueError("task counts must be positive")
            if expected_type is list and (
                not value or any(not isinstance(item, str) or not item for item in value)
            ):
                raise ValueError("capability/tool identifiers must be non-empty strings")
        if "require_dependencies" in self.parameters and type(
            self.parameters["require_dependencies"]
        ) is not bool:
            raise ValueError("require_dependencies must be bool")
        if "max_calls" in self.parameters and (
            type(self.parameters["max_calls"]) is not int or self.parameters["max_calls"] < 1
        ):
            raise ValueError("max_calls must be a positive integer")
        return self


class AgentReportCriterion(Contract):
    criterion_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    description: str = Field(min_length=1, max_length=2000)
    dimension: Literal["task_outcome", "citation_support"] = "task_outcome"
    required: bool = True
    gold_status: Literal["verified", "draft"] = "draft"


class AgentScenario(Contract):
    case_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    query: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list)
    requires_knowledge: bool = False
    gold_status: Literal["verified", "draft"] = "draft"
    source_gold_status: Literal["verified", "draft", "not_applicable"] = "not_applicable"
    expected_sources: list[SourceAnchor] = Field(default_factory=list)
    expected_facts: list[str] = Field(default_factory=list)
    assertions: list[AgentAssertion] = Field(default_factory=list)
    report_rubric: list[AgentReportCriterion] = Field(default_factory=list)
    policy_requirements: dict[str, Any] = Field(default_factory=dict)
    protocol: Literal["agent-task-v2", "agent-contract-v2"] = "agent-task-v2"

    @model_validator(mode="after")
    def validate_scenario(self):
        for values, name in (
            ([item.assertion_id for item in self.assertions], "assertion"),
            ([item.criterion_id for item in self.report_rubric], "criterion"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {name} ids")
        RunPolicy.model_validate(self.policy_requirements)
        if self.source_gold_status == "verified" and not self.expected_sources:
            raise ValueError("verified source labels require actual source locators")
        if self.gold_status == "verified" and any(
            item.gold_status != "verified" for item in self.report_rubric if item.required
        ):
            raise ValueError("scenario outcome cannot be verified with draft required rubric")
        return self


class AgentVariant(Contract):
    variant_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    policy: RunPolicy = Field(default_factory=lambda: RunPolicy(knowledge_mode="off"))
    model: str | None = Field(default=None, min_length=1, max_length=200)
    tool_allowlist: list[str] | None = Field(default=None, max_length=64)
    workflow_version: Literal["current"] = "current"
    prompt_version: Literal["current"] = "current"

    @model_validator(mode="after")
    def validate_identifiers(self):
        if self.model and (
            "://" in self.model or any(character.isspace() for character in self.model)
        ):
            raise ValueError("model must be a configured-provider model identifier, not a URL")
        if self.tool_allowlist is not None:
            if len(self.tool_allowlist) != len(set(self.tool_allowlist)):
                raise ValueError("duplicate tool identifiers")
            for name in self.tool_allowlist:
                if not name or not name.replace("_", "").isalnum() or not name[0].isalpha():
                    raise ValueError("invalid tool identifier")
        return self


def default_agent_budget() -> EvaluationBudgetSpec:
    return EvaluationBudgetSpec(case_timeout_seconds=600, concurrency=1)


class AgentExperimentPlan(Contract):
    kind: Literal["agent"] = "agent"
    # Existing v2 plans remain general; the UI explicitly opts into tool ablation.
    comparison_focus: Literal["general", "tools"] = "general"
    protocol: Literal["agent-task-v2", "agent-contract-v2"] = "agent-task-v2"
    dataset_id: str = "llm-agent-v2"
    mode: Literal["replay", "live"] = "replay"
    case_ids: list[str] = Field(default_factory=list, max_length=50)
    variants: list[AgentVariant] = Field(min_length=1, max_length=6)
    budget: EvaluationBudgetSpec = Field(default_factory=default_agent_budget)
    repetitions: int = Field(default=1, ge=1, le=5)
    space: str | None = None
    space_version_id: str | None = None
    input_refs: dict[str, str] = Field(default_factory=dict)
    allow_degraded: bool = False
    confirm_live: bool = False
    judge_enabled: bool = False
    confirm_judge: bool = False
    langsmith_sync: bool = False

    @model_validator(mode="after")
    def validate_plan(self):
        if self.mode == "live" and not self.confirm_live:
            raise ValueError("live Agent evaluation requires confirm_live=true")
        if self.judge_enabled and not self.confirm_judge:
            raise ValueError("Judge requests require confirm_judge=true")
        if len({variant.variant_id for variant in self.variants}) != len(self.variants):
            raise ValueError("duplicate Agent variant ids")
        if len(self.case_ids) != len(set(self.case_ids)):
            raise ValueError("duplicate Agent case ids")
        if self.space_version_id and not self.space:
            raise ValueError("space_version_id requires space")
        if self.comparison_focus == "tools":
            if len(self.variants) < 2:
                raise ValueError("tool comparison requires at least two variants")
            shared = self.variants[0].model_dump(exclude={"variant_id", "name", "tool_allowlist"})
            if any(item.model_dump(exclude={"variant_id", "name", "tool_allowlist"}) != shared
                   for item in self.variants[1:]):
                raise ValueError("tool comparison must keep model, policy, workflow and prompt identical")
            lists = {None if item.tool_allowlist is None else tuple(sorted(item.tool_allowlist))
                     for item in self.variants}
            if len(lists) < 2:
                raise ValueError("tool comparison requires different tool allowlists")
        return self
