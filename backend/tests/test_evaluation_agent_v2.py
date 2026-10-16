"""Agent-v2 grader regressions: fixtures are not claimed as live Agent quality."""
from __future__ import annotations

from copy import deepcopy

import pytest
from atlasflow.evaluation.agent.assertions import evaluate_assertion, ordered_events
from atlasflow.evaluation.agent.contracts import AgentAssertion, AgentExperimentPlan, AgentScenario
from atlasflow.evaluation.agent.report import summarize_agent
from atlasflow.evaluation.agent.runner import evaluate_agent, load_agent_dataset
from atlasflow.evaluation.contracts import fingerprint
from pydantic import ValidationError


def event(kind, *, task=None, data=None, status=None, version=1, node=None):
    return {"event_type": kind, "plan_version": version, "task_id": task,
            "data": data or {}, "status": status, "node": node}


def artifact(events, **run_changes):
    ordered = [event("run_started"), *events, event("run_completed")]
    for index, item in enumerate(ordered, start=1):
        item["sequence"] = index
        item["event_id"] = f"e{index}"
    return {"content": {"id": "run-1", "status": "completed", "report": "A detailed research report.",
                        "policy": {"max_tool_calls_per_run": 20}, "tool_calls": [], **run_changes},
            "events": ordered}


def assertion(kind, **parameters):
    return {"assertion_id": "fixture-check", "kind": kind, "parameters": parameters, "required": True}


def scenario(**changes):
    return {"case_id": "fixture", "query": "Explain attention using supplied evidence.",
            "gold_status": "verified", "protocol": "agent-task-v2", "assertions": [],
            "report_rubric": [{"criterion_id": "objective", "description": "Explain the actual mechanism accurately.",
                               "gold_status": "verified"}], **changes}


def grade(run, *, value=True, **changes):
    return {"criterion_id": "objective", "value": value, "status": "evaluated",
            "method": "manual", "grader_id": "fixture-manual-label", "grader_version": "1",
            "reason": "The annotated output satisfies the reference objective.",
            "evidence_refs": ["run:report"], "artifact_hash": fingerprint(run), **changes}


def test_agent_plan_rejects_rag_parameters_and_unconfirmed_fees():
    base = {"dataset_id": "llm-agent-v2", "variants": [{"variant_id": "baseline", "name": "Baseline"}]}
    parsed = AgentExperimentPlan.model_validate(base)
    assert parsed.variants[0].policy.knowledge_mode == "off"
    assert parsed.space is None
    assert parsed.budget.case_timeout_seconds == 600
    assert parsed.budget.concurrency == 1
    assert parsed.budget.max_http_attempts == 60
    assert parsed.budget.max_elapsed_seconds == 1200
    for extra in ({"tuning": {}}, {"arms": []}, {"mode": "live"}, {"judge_enabled": True}):
        with pytest.raises(ValidationError):
            AgentExperimentPlan.model_validate({**base, **extra})
    with pytest.raises(ValidationError):
        AgentExperimentPlan.model_validate({**base, "variants": [{"variant_id": "x", "name": "X", "model": "https://evil.test/api"}]})


def test_scenario_and_assertion_validate_structured_contracts():
    with pytest.raises(ValidationError):
        AgentAssertion.model_validate(assertion("initial_task_count", count=True))
    with pytest.raises(ValidationError):
        AgentAssertion.model_validate(assertion("approval_before_research", auto_approve=False))
    with pytest.raises(ValidationError):
        AgentScenario.model_validate(scenario(source_gold_status="verified"))
    with pytest.raises(ValidationError):
        AgentScenario.model_validate(scenario(report_rubric=[{"criterion_id": "objective", "description": "draft"}]))


def test_completed_and_nonempty_report_are_not_semantic_success():
    result = evaluate_agent(scenario(), artifact([]), {"protocol": "agent-task-v2"})
    assert result["metrics"]["execution_completed"]["value"] is True
    assert result["metrics"]["report_present"]["value"] is True
    assert result["metrics"]["task_success"]["value"] is None
    assert result["metrics"]["task_outcome_score"]["value"] is None


def test_manual_grade_can_fail_completed_run_and_stale_grade_is_not_used():
    frozen = artifact([])
    frozen["rubric_grades"] = [grade(frozen["content"], value=False)]
    result = evaluate_agent(scenario(), frozen, {})
    assert result["metrics"]["task_success"]["value"] is False
    frozen["rubric_grades"][0]["artifact_hash"] = "stale"
    result = evaluate_agent(scenario(), frozen, {})
    assert result["metrics"]["task_success"]["value"] is None
    assert result["checks"][0]["status"] == "grader_failed"


def test_grade_requires_reason_real_refs_version_and_exact_artifact():
    frozen = artifact([])
    for invalid in ({"reason": ""}, {"evidence_refs": ["invented:source"]}, {"grader_version": ""}, {"value": float("nan")}):
        frozen["rubric_grades"] = [grade(frozen["content"], **invalid)]
        result = evaluate_agent(scenario(), frozen, {})
        assert result["metrics"]["task_success"]["value"] is None
    frozen["rubric_grades"] = [grade(frozen["content"])]
    assert evaluate_agent(scenario(), frozen, {})["metrics"]["task_success"]["value"] is True


def test_partial_semantic_coverage_does_not_pass_all_objectives():
    case = scenario()
    case["report_rubric"].append({"criterion_id": "other", "description": "Second mandatory objective", "gold_status": "verified"})
    frozen = artifact([])
    frozen["rubric_grades"] = [grade(frozen["content"])]
    result = evaluate_agent(case, frozen, {})
    assert result["metrics"]["task_outcome_score"]["value"] == 1
    assert result["metrics"]["task_outcome_score"]["denominator"] == 1
    assert result["metrics"]["task_success"]["value"] is None


def test_draft_source_is_not_verified_semantic_gold():
    case = scenario(gold_status="draft")
    frozen = artifact([])
    frozen["rubric_grades"] = [grade(frozen["content"])]
    result = evaluate_agent(case, frozen, {})
    assert result["metrics"]["task_success"]["value"] is None
    assert result["metrics"]["task_success"]["status"] == "missing_gold"


def supplement_artifact():
    tasks = [{"task_id": "T1"}, {"task_id": "T2"}]
    return artifact([
        event("plan_created", data={"tasks": tasks}),
        event("review_decided", data={"decision": "supplement", "supplemental_tasks": [{"task_id": "T3"}]}),
        event("task_completed", task="T3", status="succeeded"),
        event("review_decided", data={"decision": "accept", "active_plan": {"tasks": [*tasks, {"task_id": "T3"}]}}),
    ])


def test_two_three_accept_uses_initial_and_supplement_delta_not_final_count():
    frozen = supplement_artifact()
    rule = assertion("supplement_sequence", initial_count=2, added_count=1)
    assert evaluate_assertion(rule, frozen)["value"] is True
    frozen["events"][1]["data"]["tasks"].append({"task_id": "T4"})
    assert evaluate_assertion(rule, frozen)["value"] is False


def test_accept_before_supplement_completion_and_intervening_replan_fail():
    rule = assertion("supplement_sequence", initial_count=2, added_count=1)
    frozen = supplement_artifact()
    frozen["events"][3]["status"] = "failed"
    assert evaluate_assertion(rule, frozen)["value"] is False
    frozen = supplement_artifact()
    frozen["events"].insert(4, event("review_decided", data={"decision": "replan"}))
    for index, item in enumerate(frozen["events"], start=1):
        item["sequence"] = index
    assert evaluate_assertion(rule, frozen)["value"] is False


def test_dependency_requires_real_completion_before_dispatch():
    rule = assertion("dependency_order", require_dependencies=True)
    frozen = artifact([
        event("task_scheduled", task="T1", data={"dependencies": []}),
        event("task_completed", task="T1", status="succeeded"),
        event("task_scheduled", task="T2", data={"dependencies": ["T1"]}),
    ])
    assert evaluate_assertion(rule, frozen)["value"] is True
    frozen["events"][2]["status"] = "failed"
    assert evaluate_assertion(rule, frozen)["value"] is False
    frozen["events"] = frozen["events"][1:-1]
    assert evaluate_assertion(rule, frozen)["value"] is None


def test_dependency_checks_actual_plan_and_research_start_not_only_schedule_labels():
    rule = assertion("dependency_order", require_dependencies=True)
    frozen = artifact([
        event("plan_created", data={"tasks": [{"task_id": "T1", "dependencies": []}, {"task_id": "T2", "dependencies": ["T1"]}]}),
        event("task_scheduled", task="T2", data={"dependencies": []}),
    ])
    assert evaluate_assertion(rule, frozen)["value"] is False
    frozen["events"][2] = {**event("node_started", task="T2", node="researcher"), "sequence": 3, "event_id": "e3"}
    assert evaluate_assertion(rule, frozen)["value"] is False


def test_approval_does_not_pass_from_auto_approve_false():
    rule = assertion("approval_before_research")
    frozen = {"content": {"auto_approve": False, "status": "completed"}, "events": []}
    assert evaluate_assertion(rule, frozen)["value"] is None
    frozen = artifact([event("task_scheduled", task="T1", data={"dependencies": []})], auto_approve=False)
    assert evaluate_assertion(rule, frozen)["value"] is False
    frozen = artifact([
        event("approval_required"), event("approval_resolved", data={"action": "approve"}),
        event("task_scheduled", task="T1", data={"dependencies": []}),
    ])
    assert evaluate_assertion(rule, frozen)["value"] is True


def test_replan_is_conditional_and_critical_issue_must_match():
    rule = assertion("replan_requires_critical_issue")
    assert evaluate_assertion(rule, artifact([event("review_decided", data={"decision": "accept"})]))["value"] is True
    frozen = artifact([event("review_decided", data={"decision": "replan", "replan_reason": "invalid_decomposition", "issues": []})])
    assert evaluate_assertion(rule, frozen)["value"] is False
    frozen["events"][1]["data"]["issues"] = [{"severity": "critical", "code": "invalid_decomposition"}]
    assert evaluate_assertion(rule, frozen)["value"] is True


def tool_artifact(*, retryable=False, cached=False):
    records, events = [], []
    for index in range(2):
        call_id = f"call-{index}"
        record = {"call_id": call_id, "tool_name": "web_search", "success": False,
                  "task_id": "T1", "arguments": {"query": "same"}, "retryable": retryable,
                  "failure_scope": None if retryable else "same-target",
                  "reused_from_call_id": "prior" if cached else None}
        records.append(record)
        events.extend([
            event("tool_requested", data={"call_id": call_id, "tool_name": "web_search"}),
            event("tool_failed", data={"call_id": call_id, "tool_name": "web_search", "reused_from_call_id": record["reused_from_call_id"]}),
        ])
    return artifact(events, tool_calls=records)


def test_tool_retries_cache_and_budget_are_distinct():
    rule = assertion("nonretryable_failure_not_repeated")
    assert evaluate_assertion(rule, tool_artifact())["value"] is False
    assert evaluate_assertion(rule, tool_artifact(retryable=True))["value"] is True
    frozen = tool_artifact(cached=True)
    metrics = evaluate_agent(scenario(), frozen, {})["metrics"]
    assert metrics["tool_executions"]["value"] == 0
    assert metrics["tool_budget_reserved"]["value"] == 2
    assert evaluate_assertion(assertion("tool_budget", max_calls=1), frozen)["value"] is False


def test_failure_scope_is_run_wide_and_missing_scope_is_not_a_pass():
    rule = assertion("nonretryable_failure_not_repeated")
    frozen = tool_artifact()
    frozen["content"]["tool_calls"][1]["task_id"] = "T2"
    assert evaluate_assertion(rule, frozen)["value"] is False
    frozen["content"]["tool_calls"][1]["failure_scope"] = None
    assert evaluate_assertion(rule, frozen)["value"] is None


def test_rejected_and_suppressed_calls_are_not_execution():
    frozen = artifact([
        event("tool_failed", data={"call_id": "bad", "executed": False, "budget_counted": False}),
        event("tool_failed", data={"call_id": "suppressed", "suppressed": True}),
    ])
    result = evaluate_agent(scenario(), frozen, {})
    assert result["metrics"]["tool_executions"]["value"] == 0
    assert result["metrics"]["tool_budget_reserved"]["value"] == 0


def test_recovery_counter_is_not_cross_process_proof():
    rule = assertion("recovery_evidence")
    frozen = artifact([event("run_interrupted"), event("run_resumed")], recovery={"recovery_count": 3})
    assert evaluate_assertion(rule, frozen)["value"] is None
    frozen["recovery_evidence"] = {
        "provenance": "isolated_postgres_process_restart", "run_id": "run-1", "checkpoint_id": "cp-1",
        "before": {"process_id": 10, "completed_invocation_ids": ["llm-1"]},
        "after": {"process_id": 11, "run_id": "run-1", "replayed_invocation_ids": ["llm-1"], "redispatched_invocation_ids": []},
    }
    assert evaluate_assertion(rule, frozen)["value"] is True
    frozen["recovery_evidence"]["after"]["redispatched_invocation_ids"] = ["llm-1"]
    assert evaluate_assertion(rule, frozen)["value"] is False


def test_citation_support_cannot_be_filled_from_validity_or_generic_report_ref():
    case = scenario(report_rubric=[{"criterion_id": "citation", "description": "Support the claim", "dimension": "citation_support", "gold_status": "verified"}])
    frozen = artifact([], evidence=[{"id": "source-evidence", "source_id": "actual-source"}])
    frozen["rubric_grades"] = [grade(frozen["content"], criterion_id="citation")]
    result = evaluate_agent(case, frozen, {})
    assert result["metrics"]["citation_support_score"]["value"] is None
    frozen["rubric_grades"] = [grade(frozen["content"], criterion_id="citation", evidence_refs=["evidence:source-evidence"])]
    assert evaluate_agent(case, frozen, {})["metrics"]["citation_support_score"]["value"] == 1


def test_dataset_labels_and_protocols_are_separate_from_legacy():
    dataset = load_agent_dataset()
    assert dataset["dataset_id"] == "llm-agent-v2"
    assert len(dataset["cases"]) == 10
    assert sum(case["protocol"] == "agent-task-v2" for case in dataset["cases"]) == 8
    assert all(case["gold_status"] == "draft" for case in dataset["cases"])
    assert dataset["cases"][0]["source_gold_status"] == "verified"
    assert dataset["cases"][1]["policy_requirements"]["supplement_task_count"] == 1


def test_summary_preserves_null_and_independent_dimensions():
    frozen = artifact([])
    result = evaluate_agent(scenario(), frozen, {})
    rows = [{"case_id": "fixture", "variant_id": "baseline", "trial": trial, **deepcopy(result)} for trial in (1, 2)]
    summary = summarize_agent(rows, expected_count=2)
    assert summary["overall_score"] is None
    assert summary["variants"]["baseline"]["metrics"]["task_success"]["mean"] is None
    assert summary["variants"]["baseline"]["fully_graded_scenario_count"] == 0


def test_duplicate_and_unordered_sequences_are_not_assumed_complete():
    frozen = artifact([])
    frozen["events"][1]["sequence"] = 1
    assert ordered_events(frozen) == ([], False)
