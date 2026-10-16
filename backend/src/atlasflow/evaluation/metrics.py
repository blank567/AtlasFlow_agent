from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict

from atlasflow.evaluation.contracts import (
    EvaluationCase,
    EvaluationCaseResult,
    FrozenEvaluationInput,
    fingerprint,
)


def _text(value: str) -> str:
    return " ".join(re.sub(r"[*`]+", "", unicodedata.normalize("NFKC", value)).casefold().split())


def evaluate_input(
    case: EvaluationCase, frozen: FrozenEvaluationInput, *, replayed: bool
) -> EvaluationCaseResult:
    metrics: dict[str, float | int | bool | None] = {
        "answer_correctness": None,
        "citation_support": None,
    }
    availability = {"answer_correctness": "not_evaluated", "citation_support": "not_evaluated"}
    failures: list[str] = []
    content = frozen.content
    source_duration = None
    degraded: list[str] = []
    if case.kind == "rag":
        from atlasflow.knowledge.domain import SearchResult

        result = SearchResult.model_validate(content)
        source_duration = result.trace.duration_ms
        degraded = result.trace.degraded
        relevant_sources = {item.source_id for item in case.expected_sources}
        ranked: list[tuple[str, str]] = []
        matched_anchors: set[int] = set()
        context_anchors: set[int] = set()
        identity_complete = True
        for hit in result.hits:
            chunk = hit.chunk
            source = frozen.source_id_map.get(chunk.document_id)
            digest = frozen.source_hash_map.get(chunk.version_id)
            if not source or not digest:
                identity_complete = False
                continue
            if not any(item[0] == source for item in ranked):
                if len(ranked) >= 5:
                    break
                ranked.append((source, digest))
            for index, gold in enumerate(case.expected_sources):
                if source != gold.source_id or digest != gold.content_hash:
                    continue
                if gold.page_number is not None and gold.page_number != chunk.page_number:
                    continue
                if gold.heading and gold.heading not in chunk.heading_path:
                    continue
                anchor = _text(gold.anchor)
                if anchor in _text(chunk.content):
                    matched_anchors.add(index)
                if anchor in _text(chunk.content + "\n" + (hit.context_excerpt or "")):
                    context_anchors.add(index)
            if len(ranked) >= 5:
                break
        metrics["evidence_count"] = len(result.hits)
        metrics["claims_sufficient"] = result.decision.status == "sufficient"
        if not case.answerable:
            availability.update(
                source_recall_at_5="not_applicable",
                mrr="not_applicable",
                anchor_hit_rate="not_applicable",
                context_anchor_hit_rate="not_applicable",
            )
            metrics.update(
                source_recall_at_5=None,
                mrr=None,
                anchor_hit_rate=None,
                context_anchor_hit_rate=None,
            )
        elif case.gold_status != "verified" or not identity_complete:
            reason = "draft_gold" if case.gold_status != "verified" else "source_identity_missing"
            availability.update(
                source_recall_at_5=reason,
                mrr=reason,
                anchor_hit_rate=reason,
                context_anchor_hit_rate=reason,
            )
            metrics.update(
                source_recall_at_5=None,
                mrr=None,
                anchor_hit_rate=None,
                context_anchor_hit_rate=None,
            )
        else:
            relevant = {(gold.source_id, gold.content_hash) for gold in case.expected_sources}
            matching = [source for source, digest in ranked if (source, digest) in relevant]
            metrics["source_recall_at_5"] = len(set(matching)) / len(relevant_sources)
            metrics["mrr"] = next(
                (1 / rank for rank, item in enumerate(ranked, 1) if item in relevant), 0.0
            )
            metrics["anchor_hit_rate"] = len(matched_anchors) / len(case.expected_sources)
            metrics["context_anchor_hit_rate"] = len(context_anchors) / len(case.expected_sources)
            availability.update(
                source_recall_at_5="evaluated",
                mrr="evaluated",
                anchor_hit_rate="evaluated_child_text",
                context_anchor_hit_rate="evaluated_child_plus_context",
            )
    else:
        from atlasflow.agents.report_quality import audit_report
        from atlasflow.schemas import RunRecord

        run = RunRecord.model_validate_json(json.dumps(content))
        source_duration = run.metrics.duration_ms
        completed = run.status.value in {"completed", "completed_with_warnings"}
        metrics.update(
            run_completed=completed,
            plan_present=run.plan is not None,
            report_nonempty=bool(run.report),
            tool_calls=len(run.tool_calls),
            tool_failures=sum(not call.success for call in run.tool_calls),
            route_count=len(run.route_history),
            recovery_count=run.recovery.recovery_count,
        )
        if not completed:
            failures.append("run_not_completed")
        if run.plan is None:
            failures.append("plan_missing")
        if not run.report:
            failures.append("report_missing")
        if len(run.tool_calls) > run.policy.max_tool_calls_per_run:
            failures.append("tool_budget_exceeded")
        if run.plan and run.report:
            issues = audit_report(
                run.report, run.plan, run.research_results, run.evidence, run.tool_calls
            )
            illegal = {
                "report_unknown_citation",
                "report_unknown_source",
                "report_internal_task_reference",
            }
            failures.extend(issue.code for issue in issues if issue.code in illegal)
            metrics["citation_contract_valid"] = not any(issue.code in illegal for issue in issues)
        else:
            metrics["citation_contract_valid"] = None
        for name in case.required_invariants:
            if name == "supplement" and run.metrics.supplement_rounds < 1:
                failures.append("expected_supplement_missing")
            elif name == "replan" and run.metrics.replan_count < 1:
                failures.append("expected_replan_missing")
            elif name == "approval" and run.auto_approve:
                failures.append("expected_manual_approval_missing")
            elif name == "recovery" and run.recovery.recovery_count < 1:
                failures.append("expected_recovery_missing")
        metrics["engineering_gate_passed"] = not failures
    return EvaluationCaseResult(
        case_id=case.case_id,
        arm_id=frozen.arm_id,
        kind=case.kind,
        status="failed" if failures else "completed",
        input_id=frozen.input_id,
        configuration_hash=frozen.config_hash,
        input_snapshot_hash=fingerprint(frozen.snapshot),
        input_provenance=frozen.provenance,
        metrics=metrics,
        availability=availability,
        invariant_failures=failures,
        replayed=replayed,
        source_duration_ms=source_duration,
        degraded=degraded,
    )


def summarize(results: list[EvaluationCaseResult], expected_count: int) -> dict:
    arms: dict[str, list[EvaluationCaseResult]] = defaultdict(list)
    for row in results:
        arms[row.arm_id].append(row)
    aggregate = {}
    for arm, rows in arms.items():
        names = {name for row in rows for name in row.metrics}
        metrics = {}
        for name in sorted(names):
            values = [float(row.metrics[name]) for row in rows if row.metrics.get(name) is not None]
            metrics[name] = {
                "mean": sum(values) / len(values) if values else None,
                "evaluated_count": len(values),
                "case_count": len(rows),
            }
        calls = [call for row in rows for call in row.provider_calls]
        costs = [call.get("usage", {}).get("cost") for call in calls if call.get("usage")]
        tokens = [call.get("usage", {}).get("total_tokens") for call in calls if call.get("usage")]
        known_costs = [value for value in costs if value is not None]
        known_tokens = [value for value in tokens if value is not None]
        aggregate[arm] = {
            "case_count": len(rows),
            "metrics": metrics,
            "failed_count": sum(row.status == "failed" for row in rows),
            "provider_calls": len(calls),
            "known_cost": sum(known_costs) if known_costs else None,
            "known_tokens": sum(known_tokens) if known_tokens else None,
            "cost_usage_coverage": len(known_costs) / len(calls) if calls else None,
            "token_usage_coverage": len(known_tokens) / len(calls) if calls else None,
        }
    return {
        "expected_count": expected_count,
        "result_count": len(results),
        "failed_count": sum(row.status == "failed" for row in results),
        "not_evaluated_count": sum(row.status == "not_evaluated" for row in results),
        "budget_stopped_count": sum(row.status == "budget_stopped" for row in results),
        "arms": aggregate,
    }
