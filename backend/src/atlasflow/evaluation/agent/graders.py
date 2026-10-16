"""Semantic grades are explicit external evidence, never keyword heuristics."""
from __future__ import annotations

import math

from atlasflow.evaluation.contracts import fingerprint

GRADER_VERSION = "2.0.0"


def metric(value, *, status=None, unit="ratio", direction="higher", scoring_unit="scenario",
           numerator=None, denominator=None, reason="", evidence_refs=None,
           grader_id="agent-offline-v2", grader_version=GRADER_VERSION):
    return {
        "value": value, "status": status or ("evaluated" if value is not None else "not_run"),
        "unit": unit, "direction": direction, "scoring_unit": scoring_unit,
        "numerator": numerator, "denominator": denominator,
        "grader_id": grader_id, "grader_version": grader_version,
        "reason": reason, "evidence_refs": evidence_refs or [],
    }


def grade_rubric(case: dict, artifact: dict) -> tuple[dict, list[dict]]:
    """Validate model/manual post-grades against the exact frozen Run artifact.

    Draft rubrics and unmatched, anonymous, stale or reason-free grades do not
    silently become a formal score. Source verification is a separate label.
    """
    run = artifact.get("content", artifact)
    artifact_hash = fingerprint(run)
    report_ref = "run:report"
    concrete_sources = {
        ref for item in run.get("evidence", [])
        for ref in (f"evidence:{item.get('id')}", f"source:{item.get('source_id')}")
        if not ref.endswith(":None")
    }
    allowed_refs = concrete_sources | {report_ref, "run:evidence", "run:tool_calls"} | {
        f"call:{item['call_id']}" for item in run.get("tool_calls", []) if item.get("call_id")
    }
    supplied = artifact.get("rubric_grades", [])
    grades = {}
    duplicates = set()
    for grade in supplied if isinstance(supplied, list) else []:
        if not isinstance(grade, dict):
            continue
        criterion_id = grade.get("criterion_id")
        if criterion_id in grades:
            duplicates.add(criterion_id)
        grades[criterion_id] = grade
    checks = []
    groups = {"task_outcome": [], "citation_support": []}
    for criterion in case.get("report_rubric", []):
        criterion_id = criterion["criterion_id"]
        grade = grades.get(criterion_id, {})
        value = grade.get("value")
        refs = grade.get("evidence_refs", [])
        valid_refs = (
            isinstance(refs, list) and bool(refs)
            and all(isinstance(ref, str) and ref in allowed_refs for ref in refs)
            and (bool(set(refs) & concrete_sources)
                 if criterion.get("dimension") == "citation_support" else report_ref in refs)
        )
        status = "not_run"
        reason = "没有独立人工或Judge语义评审；不使用关键词代替目标判断"
        trusted = (
            criterion_id not in duplicates
            and grade.get("status") == "evaluated"
            and grade.get("method") in {"manual", "judge"}
            and isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1
            and bool(grade.get("grader_id")) and bool(grade.get("grader_version"))
            and bool(str(grade.get("reason", "")).strip())
            and valid_refs
            and grade.get("artifact_hash") == artifact_hash
        )
        # Booleans are valid explicit rubric verdicts as well as normalized scores.
        if isinstance(value, bool):
            trusted = (
                criterion_id not in duplicates and grade.get("status") == "evaluated"
                and grade.get("method") in {"manual", "judge"}
                and bool(grade.get("grader_id")) and bool(grade.get("grader_version"))
                and bool(str(grade.get("reason", "")).strip())
                and valid_refs
                and grade.get("artifact_hash") == artifact_hash
            )
        formal_gold = case.get("gold_status") == "verified" and criterion.get("gold_status") == "verified"
        if not formal_gold:
            status, reason = "missing_gold", "场景或rubric尚未审核；原文source已核验不等于任务答案已核验"
        elif trusted:
            status, reason = "evaluated", grade["reason"]
        elif grade:
            status, reason = "grader_failed", "评审缺少版本、理由、证据、正确产物指纹，或值不合法/ID重复"
        evaluated_value = float(value) if status == "evaluated" else None
        check = {
            "check_id": f"rubric:{criterion_id}", "criterion_id": criterion_id,
            "dimension": criterion.get("dimension", "task_outcome"),
            "description": criterion.get("description", ""),
            "required": criterion.get("required", True), "value": evaluated_value,
            "status": status, "reason": reason,
            "evidence_refs": grade.get("evidence_refs", []) if trusted else [],
            "grader_id": grade.get("grader_id", "external-semantic-grader"),
            "grader_version": grade.get("grader_version", "unavailable"),
        }
        checks.append(check)
        groups[check["dimension"]].append(check)
    metrics = {}
    for dimension, key in (("task_outcome", "task_outcome_score"), ("citation_support", "citation_support_score")):
        values = [item["value"] for item in groups[dimension] if item["value"] is not None]
        metrics[key] = metric(
            sum(values) / len(values) if values else None,
            status="evaluated" if values else "missing_gold" if any(item["status"] == "missing_gold" for item in groups[dimension]) else "not_run",
            scoring_unit="rubric_criterion", numerator=sum(values) if values else None,
            denominator=len(values),
            reason=f"已评{len(values)}/{len(groups[dimension])}条；缺评项不填0，部分均值不代表全部目标完成",
            evidence_refs=[ref for item in groups[dimension] if item["value"] is not None for ref in item["evidence_refs"]],
        )
    required = [item for item in groups["task_outcome"] if item["required"]]
    observed = [item for item in required if item["value"] is not None]
    if not required:
        success, status, reason = None, "missing_gold", "没有显式的必要任务目标rubric"
    elif any(item["value"] < 1 for item in observed):
        success, status, reason = False, "evaluated", "至少一个必要任务目标经可靠评审未完全满足"
    elif len(observed) < len(required):
        success = None
        status = "missing_gold" if any(item["status"] == "missing_gold" for item in required) else "not_run"
        reason = "必要任务目标尚未全部获得可靠语义评审；completed不能替代成功"
    else:
        success = run.get("status") in {"completed", "completed_with_warnings"} and bool(str(run.get("report") or "").strip())
        status, reason = "evaluated", "全部必要目标均获满分，另核验终态和交付物"
    metrics["task_success"] = metric(success, status=status, unit="boolean", reason=reason,
                                      evidence_refs=[ref for item in observed for ref in item["evidence_refs"]])
    return metrics, checks
