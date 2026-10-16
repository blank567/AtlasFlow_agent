"""Pure scoring entry point; network execution is supplied by the shared service."""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from atlasflow.evaluation.agent.assertions import evaluate_assertion, tool_observations
from atlasflow.evaluation.agent.contracts import AgentScenario
from atlasflow.evaluation.agent.graders import grade_rubric, metric


def load_agent_dataset() -> dict:
    root = Path(__file__).resolve().parents[5]
    path = root / "evals" / "agent" / "dataset-v2.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        resource = resources.files("atlasflow.evaluation").joinpath("data/agent-v2.json")
        raw = json.loads(resource.read_text(encoding="utf-8"))
    scenarios = [AgentScenario.model_validate(case) for case in raw["cases"]]
    if raw.get("kind") != "agent" or len({case.case_id for case in scenarios}) != len(scenarios):
        raise ValueError("invalid Agent scenario dataset kind or duplicate cases")
    return {**raw, "cases": [case.model_dump(mode="json") for case in scenarios]}


def _aggregate_checks(checks: list[dict], dimension: str) -> dict:
    required = [check for check in checks if check["dimension"] == dimension and check["required"]]
    evaluated = [check for check in required if check["value"] is not None]
    if not required:
        return metric(None, status="not_applicable", unit="boolean", reason="场景未声明此维度的必需工程约束")
    value = False if any(check["value"] is False for check in evaluated) else True if len(evaluated) == len(required) else None
    return metric(value, unit="boolean", scoring_unit="required_assertion",
                  numerator=sum(check["value"] is True for check in evaluated), denominator=len(required),
                  reason=f"已核验{len(evaluated)}/{len(required)}条；缺少观察不算通过",
                  evidence_refs=[ref for check in evaluated for ref in check["evidence_refs"]])


def _citation_validity(run: dict) -> dict:
    if not run.get("report"):
        return metric(None, status="not_run", unit="boolean", reason="没有报告产物")
    try:
        from atlasflow.agents.contracts import ResearchPlan, ResearchResult
        from atlasflow.agents.report_quality import audit_report
        from atlasflow.schemas import Evidence, ToolCallRecord

        if not run.get("plan"):
            return metric(None, unit="boolean", reason="缺少计划与引用目录，无法执行完整引用契约校验")
        issues = audit_report(
            run["report"], ResearchPlan.model_validate(run["plan"]),
            [ResearchResult.model_validate(item) for item in run.get("research_results", [])],
            [Evidence.model_validate(item) for item in run.get("evidence", [])],
            [ToolCallRecord.model_validate(item) for item in run.get("tool_calls", [])],
        )
        codes = {"report_unknown_citation", "report_unknown_source", "report_internal_task_reference", "report_citation_missing_reference"}
        invalid = [issue.code for issue in issues if issue.code in codes]
        return metric(not invalid, unit="boolean", reason="仅核验引用编号、来源和目录契约，不证明引用支持结论" + (f"；问题：{','.join(invalid)}" if invalid else ""),
                      evidence_refs=["run:report", "run:evidence", "run:tool_calls"])
    except (TypeError, ValueError, KeyError):
        return metric(None, unit="boolean", reason="冻结Run缺少完整可校验结构，引用契约未评测")


def evaluate_agent(case: dict, artifact: dict, plan: dict) -> dict:
    """Return four independent dimensions; no cross-domain overall score."""
    scenario = AgentScenario.model_validate(case).model_dump(mode="json")
    run = artifact.get("content", artifact)
    policy = artifact.get("config_identity", {}).get("policy", run.get("policy", {}))
    checks = [
        {"check_id": f"assertion:{assertion['assertion_id']}", **evaluate_assertion(assertion, artifact, policy)}
        for assertion in scenario["assertions"]
    ]
    metrics = {
        "execution_completed": metric(
            run.get("status") in {"completed", "completed_with_warnings"} if run.get("status") else None,
            unit="boolean", reason="只表示执行终态，不代表满足研究目标", evidence_refs=["run:status"]),
        "report_present": metric(bool(str(run.get("report") or "").strip()) if "report" in run else None, unit="boolean",
                                 reason="仅检查交付物存在，不判断内容质量", evidence_refs=["run:report"]),
        "trajectory_compliance": _aggregate_checks(checks, "trajectory"),
        "tool_policy_compliance": _aggregate_checks(checks, "tools"),
        "citation_validity": _citation_validity(run),
    }
    semantic_metrics, semantic_checks = grade_rubric(scenario, artifact)
    metrics.update(semantic_metrics)
    checks.extend(semantic_checks)
    tools = tool_observations(artifact)
    actual = tools["executed"]
    metrics["tool_executions"] = metric(len(actual) if tools["coverage"] else None, unit="count", direction="neutral",
                                        reason="仅实际执行且有请求/终态/调用记录的工具；排除缓存复用、拒绝与抑制重试", evidence_refs=[f"call:{call['call_id']}" for call in actual])
    metrics["tool_failures"] = metric(sum(not call.get("success") for call in actual) if tools["coverage"] else None,
                                      unit="count", direction="lower", reason="仅实际执行的业务失败，不包含请求校验拒绝")
    metrics["tool_budget_reserved"] = metric(len(tools["reserved"]) if tools["complete"] else None,
                                             unit="count", direction="neutral", reason="预算预留与实际执行分开；请求前已通过预算预留")
    efficiency = run.get("metrics", {})
    usage = run.get("recovery", {}).get("provider_usage", {})
    for key, value, unit in (
        ("duration_ms", efficiency.get("duration_ms"), "ms"),
        ("model_calls", usage.get("model_calls", efficiency.get("model_calls")), "count"),
        ("http_attempts", usage.get("http_attempts"), "count"),
        ("known_tokens", usage.get("known_tokens"), "tokens"),
        ("known_cost", usage.get("known_cost"), "USD"),
    ):
        metrics[key] = metric(value, unit=unit, direction="lower", reason="记录已知消耗；缺失不填0；更少消耗不等于更高质量", evidence_refs=["run:metrics", "run:recovery.provider_usage"])
    statuses = [check["status"] for check in semantic_checks]
    semantic_status = "completed" if statuses and all(status == "evaluated" for status in statuses) else "partial" if "evaluated" in statuses else "missing_gold" if "missing_gold" in statuses else "not_run"
    return {
        "metrics": metrics, "checks": checks,
        "stage_status": {
            "execution": run.get("status", "not_run"),
            "trajectory": "completed" if tools["complete"] else "partial" if tools["events"] else "not_run",
            "semantic_grading": semantic_status,
        },
        "diagnostics": {
            "observed_tool_executions": len(actual), "event_log_complete": tools["complete"],
            "unconfirmed_tool_calls": tools["unknown_call_ids"],
            "protocol": plan.get("protocol", scenario["protocol"]),
            "limitations": [
                "completed不等于任务成功；确定性工程约束不与语义质量求总分",
                "缺少真实跨进程证据时恢复约束未评测",
                "引用合法不等于引用支持结论；草稿rubric不进入正式质量均值",
            ],
        },
    }
