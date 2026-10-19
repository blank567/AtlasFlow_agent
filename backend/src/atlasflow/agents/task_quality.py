"""Task acceptance and local-repair checks, independent of the model/provider.

Confidence and criterion coverage are distinct, NOT proof of factual truth.
These checks cannot prove semantic entailment; Critic/QualityGate still do that.
"""

from __future__ import annotations

from collections.abc import Sequence

from atlasflow.agents.contracts import (
    CritiqueDecision,
    CritiqueRoute,
    ResearchPlan,
    ResearchResult,
    ResearchTask,
    ReviewContext,
    Severity,
)
from atlasflow.schemas import Evidence, ToolCallRecord

NAVIGATION_CAPABILITIES = frozenset({"map_route", "map_itinerary"})


def has_substantive_text(value: object) -> bool:
    """Exclude empty/symbol-only placeholders, not judge semantic correctness."""
    return isinstance(value, str) and any(char.isalnum() for char in value)


def execution_accepted(result: ResearchResult) -> bool:
    """Count accepted partial work as success without rewriting its evidence status."""
    if result.execution_accepted is not None:
        return result.execution_accepted
    return result.outcome != "partial"


def can_degrade_critic_review(
    plan: ResearchPlan, results: Sequence[ResearchResult], context: ReviewContext
) -> bool:
    """Require explicit current acceptance, including any fully validated repair.

    Legacy/unassessed results and a user-mandated pending supplement cannot
    authorize skipping a malformed review. QualityGate must still run later.
    """
    if (
        context.plan_version != plan.plan_version
        or context.supplement_rounds_used < context.policy.required_supplement_rounds
    ):
        return False
    accepted_results = [
        item
        for item in results
        if item.execution_accepted is True and item.plan_version == plan.plan_version
    ]
    accepted_ids = {item.task_id for item in accepted_results}
    accepted_ids.update(repaired_task_ids(plan, accepted_results))
    return set(plan.task_ids) <= accepted_ids


def accept_noncritical_supplement(
    decision: CritiqueDecision,
    plan: ResearchPlan,
    results: Sequence[ResearchResult],
    context: ReviewContext,
) -> CritiqueDecision:
    """A presentation run need not expand accepted work for optional improvements."""
    accepted = {item.task_id for item in results if execution_accepted(item)}
    accepted.update(repaired_task_ids(plan, results))
    if (
        decision.decision is CritiqueRoute.SUPPLEMENT
        and context.supplement_rounds_used >= context.policy.required_supplement_rounds
        and set(plan.task_ids) <= accepted
        and not any(item.severity is Severity.CRITICAL for item in decision.issues)
    ):
        return CritiqueDecision.model_validate(
            {
                **decision.model_dump(mode="python"),
                "decision": CritiqueRoute.ACCEPT,
                "supplemental_tasks": [],
                "rationale": (
                    decision.rationale + "；展示验收已采纳全部任务的可用结果，非关键改进不追加研究"
                )[:2000],
            }
        )
    return decision


def validate_task_result(
    task: ResearchTask, result: ResearchResult, *, require_assessments: bool = False
) -> None:
    """Reject placeholders and invalid acceptance-to-finding links on NEW execution.

    Do not put this on model loading: old runs may contain malformed findings
    and must remain inspectable for debugging.
    """

    if not has_substantive_text(result.summary):
        raise ValueError("research summary must contain substantive text, not punctuation")
    empty_indices = [
        index for index, finding in enumerate(result.findings, 1)
        if not has_substantive_text(finding)
    ]
    if empty_indices:
        raise ValueError(
            "research findings must contain substantive text, not punctuation placeholders; "
            f"invalid 1-based findings indices: {empty_indices}"
        )
    if not result.assessments and not require_assessments:
        return
    expected = set(range(1, len(task.success_criteria) + 1))
    indices = [item.criterion_index for item in result.assessments]
    if len(indices) != len(set(indices)) or set(indices) != expected:
        raise ValueError(
            "assessments must cover every success criterion exactly once using 1-based indices"
        )
    for item in result.assessments:
        if len(item.finding_indices) != len(set(item.finding_indices)) or any(
            index > len(result.findings) for index in item.finding_indices
        ):
            raise ValueError("assessment finding_indices must reference unique existing findings")
        if item.status in {"met", "partial"} and not item.finding_indices:
            raise ValueError("met/partial assessment requires concrete supporting finding_indices")
        if not has_substantive_text(item.reason):
            raise ValueError("assessment reason must explain acceptance or the missing fact")


def calibrate_task_result(
    task: ResearchTask,
    result: ResearchResult,
    *,
    records: Sequence[ToolCallRecord] = (),
    evidence: Sequence[Evidence] = (),
    required_tools_missing: bool = False,
    accept_partial_results: bool = False,
) -> ResearchResult:
    """Keep reliability distinct from coverage when partial results are accepted."""

    validate_task_result(task, result)
    if not result.assessments:
        # Backwards-compatible custom gateways. The production gateway requires
        # complete assessments before returning, unlike legacy test fixtures.
        return result
    assessments = list(result.assessments)
    grounded_evidence = [
        item
        for item in evidence
        if item.uri
        and (not task.requires_fresh_data or item.uri.startswith(("https://", "http://")))
    ]
    urls = {item.uri for item in grounded_evidence}
    urls.update(
        url
        for call in records
        for url in call.navigation_urls or ([call.navigation_url] if call.navigation_url else [])
    )
    source_ids = {item.id for item in grounded_evidence}
    evidence_ids = set(source_ids)
    source_ids.update(
        call.call_id
        for call in records
        if call.success
        and (
            set(call.evidence_ids) & evidence_ids or (call.summary and not task.requires_fresh_data)
        )
        or call.navigation_urls
        or call.navigation_url
    )
    limits = list(result.limitations)
    for index, item in enumerate(assessments):
        findings = [result.findings[number - 1] for number in item.finding_indices]
        bound_ids = [source_id for source_id in item.source_ids if source_id in source_ids]
        if item.source_ids != bound_ids:
            assessments[index] = item.model_copy(update={"source_ids": bound_ids})
        if (
            item.status == "met"
            and task.requires_fresh_data
            and not (bound_ids or any(url in finding for url in urls for finding in findings))
        ):
            reason = "该标准未绑定本次获取的可核验来源或导航链接，不能记为已满足"
            assessments[index] = item.model_copy(
                update={"status": "partial", "reason": reason, "source_ids": bound_ids}
            )
            limits.append(f"标准{item.criterion_index}：{reason}")
    coverage = sum(
        {"met": 1.0, "partial": 0.5, "unmet": 0.0}[item.status] for item in assessments
    ) / len(assessments)
    complete = all(item.status == "met" for item in assessments) and not required_tools_missing
    cap = 1.0 if complete else 0.69
    if required_tools_missing:
        cap = 0.49
        limits.append("必需工具未全部取得可用结果，仅保留已核实部分；不得声称任务已完整完成")
    reported = (
        min(result.reported_confidence, result.confidence)
        if result.reported_confidence is not None
        else result.confidence
    )
    grounded = any(
        item.source_ids
        or any(
            url in result.findings[number - 1] for url in urls for number in item.finding_indices
        )
        for item in assessments
        if item.status in {"met", "partial"}
    )
    if accept_partial_results and coverage > 0 and (not task.requires_fresh_data or grounded):
        # Reliability describes the supported conclusions, not how much of the
        # task was covered. Do not punish an incomplete task twice or invent a
        # confidence floor. The original self-rating is still an upper bound.
        confidence = reported
    else:
        confidence = min(reported, coverage, cap)
    # Full completion remains separate from accepting useful partial work.
    complete = complete and confidence >= 0.6
    accepted_partial = (
        accept_partial_results and coverage > 0 and (not task.requires_fresh_data or grounded)
    )
    if not complete and not limits:
        limits.append("部分成功标准未满足或结论把握不足，需补充核验")
    return result.model_copy(
        update={
            "assessments": assessments,
            "outcome": "complete" if complete else "partial",
            "execution_accepted": complete or accepted_partial,
            "reported_confidence": reported,
            "confidence": round(confidence, 4),
            "criteria_coverage": round(coverage, 4),
            "limitations": list(dict.fromkeys(limits))[:20],
        }
    )


def validate_local_repairs(
    decision: CritiqueDecision, plan: ResearchPlan, context: ReviewContext
) -> None:
    """A supplement must explicitly own every task-specific critical gap.

    Keep scheduling dependencies separate so a failed task cannot block its
    own repair. A web-only task cannot claim to repair missing navigation.
    """

    if decision.decision is not CritiqueRoute.SUPPLEMENT:
        return
    existing = plan.task_map
    targets: set[str] = set()
    unavailable = set(context.failed_task_ids) | set(context.blocked_task_ids)
    for task in decision.supplemental_tasks:
        if set(task.dependencies) & unavailable:
            raise ValueError(
                "supplement cannot depend on a failed/blocked task; reuse its valid upstream dependencies"
            )
        for target_id in task.repairs_task_ids:
            if target_id not in existing:
                raise ValueError(
                    "repair target must reference an existing task in the reviewed plan"
                )
            target = existing[target_id]
            if not set(target.success_criteria) <= set(task.success_criteria):
                raise ValueError(
                    "repair must preserve the original success_criteria verbatim; do not weaken acceptance"
                )
            required_navigation = set(target.required_capabilities) & NAVIGATION_CAPABILITIES
            replacement_navigation = set(task.required_capabilities) | {
                cap for group in task.capability_alternatives for cap in group
            }
            if required_navigation and not required_navigation <= replacement_navigation:
                raise ValueError(
                    "navigation repair must retain the target's required navigation capability"
                )
            targets.add(target_id)
    critical_targets = {
        item.task_id
        for item in decision.issues
        if item.severity is Severity.CRITICAL and item.task_id in existing
    }
    missing = critical_targets - targets - set(context.repaired_task_ids)
    if missing:
        raise ValueError(
            f"supplement must explicitly cover critical task gaps in repairs_task_ids: {sorted(missing)}"
        )


def repaired_task_ids(plan: ResearchPlan, results: Sequence[ResearchResult]) -> set[str]:
    """Only fully accepted, criterion-compatible replacements close old gaps."""

    completed = {item.task_id for item in results if item.outcome == "complete"}
    repaired: set[str] = set()
    for task in plan.tasks:
        if task.task_id not in completed:
            continue
        for target in task.repairs_task_ids:
            # Exact original criteria prevent a narrower task from claiming it
            # closed ALL of a previous task. Model may expand, not weaken them.
            if set(plan.task_map[target].success_criteria) <= set(task.success_criteria):
                repaired.add(target)
    return repaired
