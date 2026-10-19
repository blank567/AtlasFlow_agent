"""Deterministic checks for facts the writing model cannot grade about itself."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from urllib.parse import quote

from atlasflow.agents.contracts import (
    QualityDecision,
    QualityRoute,
    ResearchPlan,
    ResearchResult,
    ReviewIssue,
    Severity,
)
from atlasflow.agents.report_skill import build_citation_catalog, cited_numbers, report_prose
from atlasflow.agents.report_structure import report_paragraphs
from atlasflow.schemas import Evidence, ToolCallRecord

_TASK_REFERENCE = re.compile(r"\[task:([A-Za-z0-9][A-Za-z0-9_.-]*)\]")
_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")
_REFERENCE_ENTRY = re.compile(r"^\[(\d{1,3})\]\s+", re.MULTILINE)
_REFERENCE_SECTION = re.compile(r"^##\s+(?:来源|参考文献)\s*$", re.MULTILINE)
_REQUIRED_HEADINGS = {"摘要", "结论", "局限", "参考文献"}
_LINK_SAFE_CHARS = "/:#?&=@%+;,"
# Presentation and declared optional gaps do not invalidate a supported core answer.
# Unknown citations, unknown URLs, missing sources and critical facts still block.
_NON_BLOCKING_ISSUES = frozenset(
    {
        "report_missing_sections",
        "report_internal_task_reference",
        "report_nonacademic_inline_link",
        "report_too_brief",
        "presentation_style",
        "presentation_completeness",
        "report_limitation_note",
        "report_optional_detail",
    }
)

_CORE_INVARIANTS = {"report_missing_fresh_evidence", "report_update_missing_content"}


def calibrate_review_scope(
    decision: QualityDecision, report: str, *, relaxed_review: bool,
) -> QualityDecision:
    """Calibrate explicit materiality, never infer it from a code or regex.

    Unknown/legacy scope stays conservative. Only already-declared, non-core
    limitations can be nonblocking; a local body error still needs correction.
    Original model severity remains in the audit record.
    """
    if not relaxed_review:
        return decision
    paragraphs = {paragraph.id: paragraph for paragraph in report_paragraphs(report)}
    issues: list[ReviewIssue] = []
    changed = False
    for issue in decision.issues:
        severity = issue.severity
        locations = [paragraphs[key] for key in issue.paragraph_ids if key in paragraphs]
        if issue.code not in _CORE_INVARIANTS and issue.scope == "local" and locations:
            if all(paragraph.in_limitations for paragraph in locations):
                severity = Severity.INFO
            elif severity is Severity.CRITICAL:
                severity = Severity.WARNING
        if severity is not issue.severity:
            issue = issue.model_copy(update={
                "severity": severity, "reported_severity": issue.reported_severity or issue.severity,
            })
            changed = True
        issues.append(issue)
    if not changed:
        return decision
    route = decision.decision
    instructions = list(decision.revision_instructions)
    if route is QualityRoute.REPLAN and not any(item.severity is Severity.CRITICAL for item in issues):
        route = QualityRoute.REVISE
        instructions = list(dict.fromkeys([
            *instructions, *(item.recommendation for item in issues if item.recommendation.strip()),
        ]))[:20]
    return QualityDecision.model_validate({
        **decision.model_dump(mode="python"), "issues": issues, "decision": route,
        "revision_instructions": instructions,
        "rationale": (decision.rationale + "；按显式影响范围区分局部缺陷与核心阻断，保留原始严重度")[:4000],
    })


def _nonblocking(issue: ReviewIssue) -> bool:
    return issue.severity is not Severity.CRITICAL and issue.code not in _CORE_INVARIANTS and (
        issue.code in _NON_BLOCKING_ISSUES
        or (issue.scope == "local" and issue.severity is Severity.INFO and issue.paragraph_ids
            and all(key.startswith("L") for key in issue.paragraph_ids))
    )


def report_audit_context(report: str, issues: Sequence[ReviewIssue]) -> str:
    """Give QualityGate exact formatting facts, never a semantic pass verdict."""

    prose = report_prose(report)
    section = _REFERENCE_SECTION.search(prose)
    entries = {
        int(number)
        for number in _REFERENCE_ENTRY.findall(prose[section.end() :] if section else "")
    }
    used = cited_numbers(report)
    return "\n系统生成的报告格式核验（不证明论断真实或引用支持语义）：\n" + json.dumps(
        {
            "body_citations": sorted(used),
            "reference_entries": sorted(entries),
            "missing_reference_entries": sorted(used - entries),
            "format_issues": [item.model_dump(mode="json") for item in issues],
            "bibliography_policy": "文末允许[访问链接](URL)，只列正文实际使用的来源；正文不得用来源链接代替编号引用。",
        },
        ensure_ascii=False,
    )


def audit_report(
    report: str,
    plan: ResearchPlan,
    results: Sequence[ResearchResult],
    evidence: Sequence[Evidence],
    records: Sequence[ToolCallRecord],
    *,
    source_order: Sequence[str] = (),
    relaxed_review: bool = False,
) -> list[ReviewIssue]:
    """Check section and reference contracts, not the truth of prose claims."""

    prose = report_prose(report)
    issues: list[ReviewIssue] = []

    def issue(severity: Severity, code: str, message: str, recommendation: str) -> None:
        if relaxed_review and severity is not Severity.CRITICAL and code in _NON_BLOCKING_ISSUES:
            severity = Severity.INFO
        issues.append(
            ReviewIssue(
                severity=severity,
                code=code,
                message=message,
                task_id=None,
                recommendation=recommendation,
            )
        )

    headings = {
        match.group(1).strip()
        for match in re.finditer(r"^##\s+(.+?)\s*#*\s*$", prose, flags=re.MULTILINE)
    }
    missing = _REQUIRED_HEADINGS - headings
    if missing:
        issue(
            Severity.WARNING,
            "report_missing_sections",
            f"报告缺少必要二级章节：{', '.join(sorted(missing))}",
            "补全 ## 摘要、## 结论、## 局限、## 参考文献；正文其他章节按研究问题组织",
        )

    cited_tasks = set(_TASK_REFERENCE.findall(prose))
    if cited_tasks:
        issue(
            Severity.WARNING,
            "report_internal_task_reference",
            "报告暴露了内部研究任务标记",
            "删除所有 [task:任务ID]；读者可见引用只能使用系统引用目录中的编号",
        )

    source_urls = {
        item.uri for item in evidence if item.uri and item.uri.startswith(("https://", "http://"))
    }
    source_urls.update(
        url
        for call in records
        for url in (call.navigation_urls or ([call.navigation_url] if call.navigation_url else []))
        if url.startswith(("https://", "http://"))
    )
    allowed_urls = source_urls | {quote(url, safe=_LINK_SAFE_CHARS) for url in source_urls}
    reference_match = _REFERENCE_SECTION.search(prose)
    body = prose[: reference_match.start()] if reference_match else prose
    cited_urls = set(_MARKDOWN_LINK.findall(prose))
    unknown_urls = cited_urls - allowed_urls
    if unknown_urls:
        issue(
            Severity.WARNING,
            "report_unknown_source",
            f"报告有 {len(unknown_urls)} 个链接不在已获取的工具证据或导航结果中",
            "核对并替换未验证链接；不得编造来源或导航地址",
        )
    if _MARKDOWN_LINK.search(body):
        issue(
            Severity.WARNING,
            "report_nonacademic_inline_link",
            "正文使用了 Markdown 链接而不是编号引用",
            "将正文链接替换为与系统引用目录对应的 [数字]，URL 仅保留在参考文献",
        )

    catalog = build_citation_catalog(list(evidence), list(records), source_order=source_order)
    known_numbers = {item.number for item in catalog}
    used_numbers = cited_numbers(prose)
    unknown_numbers = used_numbers - known_numbers
    if unknown_numbers:
        issue(
            Severity.WARNING,
            "report_unknown_citation",
            "报告使用了引用目录中不存在的编号："
            + ", ".join(str(number) for number in sorted(unknown_numbers)),
            "删除或替换无效编号；不得自行新增、重排或复用引用编号",
        )
    reference_numbers = {
        int(number)
        for number in _REFERENCE_ENTRY.findall(
            prose[reference_match.end() :] if reference_match else ""
        )
    }
    valid_used = used_numbers & known_numbers
    if valid_used - reference_numbers:
        issue(
            Severity.WARNING,
            "report_citation_missing_reference",
            "正文编号没有对应的参考文献条目",
            "根据系统引用目录重建参考文献，并保持编号一一对应",
        )
    if source_urls and not valid_used:
        issue(
            Severity.WARNING,
            "report_missing_source",
            "已有可核查来源，但正文没有使用有效的编号引用",
            "在相关事实附近添加最小充分的 [数字] 引用，并在参考文献列出对应来源",
        )

    body_chars = len(re.sub(r"\s+", "", body))
    if len(results) >= 3 and source_urls and body_chars < 1_200:
        issue(
            Severity.WARNING,
            "report_too_brief",
            f"多任务研究报告正文仅约 {body_chars} 个非空白字符，缺少充分展开",
            "围绕核心发现补充证据解释、比较、影响、可执行建议和结论，避免只拼接任务摘要",
        )

    if any(task.requires_fresh_data for task in plan.tasks) and not source_urls:
        issue(
            Severity.CRITICAL,
            "report_missing_fresh_evidence",
            "计划需要当前数据，但没有可核查的外部来源或导航结果",
            "重新获取当前证据；报告应明确标为未核实，不得给出当前数值结论",
        )
    return issues


def apply_report_audit(
    decision: QualityDecision,
    issues: Sequence[ReviewIssue],
    *,
    threshold: int,
    relaxed_review: bool = False,
) -> QualityDecision:
    """Preserve model judgment unless a check proves acceptance impossible."""

    if not issues and not relaxed_review:
        return decision
    merged = list(decision.issues)
    rank = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
    for audit_issue in issues:
        previous = next((index for index, item in enumerate(merged)
                         if item.code == audit_issue.code and item.task_id == audit_issue.task_id), None)
        if previous is None:
            merged.append(audit_issue)
        elif rank[audit_issue.severity] > rank[merged[previous].severity]:
            # A model label must not suppress a stronger deterministic invariant.
            merged[previous] = audit_issue
    merged = merged[:20]
    critical = any(item.severity is Severity.CRITICAL for item in merged)
    blocking = [
        item
        for item in issues
        if not (
            relaxed_review and _nonblocking(item)
        )
    ]
    route = decision.decision
    if (
        relaxed_review
        and route is QualityRoute.REVISE
        and decision.score >= threshold
        and merged
        and all(
            _nonblocking(item)
            for item in merged
        )
    ):
        route = QualityRoute.ACCEPT
    if critical:
        route = QualityRoute.REPLAN
    elif blocking and route is QualityRoute.ACCEPT:
        route = QualityRoute.REVISE
    instructions = list(decision.revision_instructions)
    for item in issues:
        if item.recommendation not in instructions and len(instructions) < 20:
            instructions.append(item.recommendation)
    return QualityDecision.model_validate(
        {
            **decision.model_dump(mode="python"),
            "decision": route,
            "score": decision.score,
            "rationale": (
                decision.rationale
                + (
                    "；展示验收保留非阻断改进项，不要求额外修订"
                    if route is QualityRoute.ACCEPT and merged
                    else "；确定性报告检查发现：" + "、".join(item.code for item in issues)
                    if issues
                    else ""
                )
            )[:4000],
            "issues": merged,
            "revision_instructions": instructions,
        }
    )


def limit_to_report_repair(decision: QualityDecision) -> QualityDecision:
    """Keep rejection/severity/score, but repair or remove claims locally.

    Used only by presentation delivery. A critical citation or opening-hours
    problem does not justify re-running every research task. If one bounded
    revision cannot fix it, deliver the complete manuscript with unverified
    passages explicitly marked; only use a source summary when no manuscript exists.
    """

    if decision.decision is not QualityRoute.REPLAN:
        return decision
    instructions = list(dict.fromkeys([
        *decision.revision_instructions,
        *(item.recommendation for item in decision.issues if item.recommendation.strip()),
        "只依据已有材料局部修正；无法核实的具体断言必须删除或明确标为未核实，不得编造来源。",
    ]))[:20]
    return decision.model_copy(update={
        "decision": QualityRoute.REVISE,
        "rationale": (decision.rationale + "；交付优先：改为一次局部修订，不重启整轮研究")[:4000],
        "revision_instructions": instructions,
    })
