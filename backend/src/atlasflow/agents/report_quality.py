"""Deterministic checks for facts the writing model cannot grade about itself."""

from __future__ import annotations

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
from atlasflow.schemas import Evidence, ToolCallRecord

_TASK_REFERENCE = re.compile(r"\[task:([A-Za-z0-9][A-Za-z0-9_.-]*)\]")
_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")
_SOURCE_HEADING = {"摘要", "局限", "来源"}
_LINK_SAFE_CHARS = "/:#?&=@%+;,"


def _without_fenced_code(report: str) -> str:
    lines: list[str] = []
    fence: str | None = None
    for line in report.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            character = marker.group(1)[0]
            if fence is None:
                fence = character
            elif fence == character:
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def audit_report(
    report: str,
    plan: ResearchPlan,
    results: Sequence[ResearchResult],
    evidence: Sequence[Evidence],
    records: Sequence[ToolCallRecord],
) -> list[ReviewIssue]:
    """Check section and reference contracts, not the truth of prose claims."""

    prose = _without_fenced_code(report)
    issues: list[ReviewIssue] = []

    def issue(severity: Severity, code: str, message: str, recommendation: str) -> None:
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
    missing = _SOURCE_HEADING - headings
    if missing:
        issue(
            Severity.WARNING,
            "report_missing_sections",
            f"报告缺少必要二级章节：{', '.join(sorted(missing))}",
            "补全 ## 摘要、## 局限、## 来源；正文其他章节按研究问题组织",
        )

    known_tasks = {result.task_id for result in results}
    cited_tasks = set(_TASK_REFERENCE.findall(prose))
    unknown_tasks = cited_tasks - known_tasks
    if unknown_tasks:
        issue(
            Severity.WARNING,
            "report_unknown_task_reference",
            f"引用了不存在的研究任务：{', '.join(sorted(unknown_tasks))}",
            "删除无效任务引用，并在相关论断附近引用真实任务 ID",
        )
    if known_tasks and not cited_tasks.intersection(known_tasks):
        issue(
            Severity.WARNING,
            "report_missing_task_reference",
            "报告没有引用任何已完成的研究任务",
            "在研究结论附近添加真实的 [task:任务ID] 引用",
        )

    source_urls = {
        item.uri
        for item in evidence
        if item.uri and item.uri.startswith(("https://", "http://"))
    }
    source_urls.update(
        url
        for call in records
        for url in (call.navigation_urls or ([call.navigation_url] if call.navigation_url else []))
        if url.startswith(("https://", "http://"))
    )
    allowed_urls = source_urls | {quote(url, safe=_LINK_SAFE_CHARS) for url in source_urls}
    cited_urls = set(_MARKDOWN_LINK.findall(prose))
    unknown_urls = cited_urls - allowed_urls
    if unknown_urls:
        issue(
            Severity.WARNING,
            "report_unknown_source",
            f"报告有 {len(unknown_urls)} 个链接不在已获取的工具证据或导航结果中",
            "核对并替换未验证链接；不得编造来源或导航地址",
        )
    if source_urls and not cited_urls.intersection(allowed_urls):
        issue(
            Severity.WARNING,
            "report_missing_source",
            "已有工具来源或导航链接，但报告没有引用任何一个",
            "在相关事实附近添加真实来源，并在 ## 来源列出完整链接",
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
    decision: QualityDecision, issues: Sequence[ReviewIssue], *, threshold: int
) -> QualityDecision:
    """Preserve model judgment unless a check proves acceptance impossible."""

    if not issues:
        return decision
    existing = {item.code for item in decision.issues}
    merged = [*decision.issues, *(item for item in issues if item.code not in existing)][:20]
    critical = any(item.severity is Severity.CRITICAL for item in merged)
    route = decision.decision
    if critical:
        route = QualityRoute.REPLAN
    elif route is QualityRoute.ACCEPT:
        route = QualityRoute.REVISE
    instructions = list(decision.revision_instructions)
    for item in issues:
        if item.recommendation not in instructions and len(instructions) < 20:
            instructions.append(item.recommendation)
    score = decision.score
    if critical:
        score = min(score, 59)
    elif route is QualityRoute.REVISE:
        score = min(score, threshold - 1)
    return QualityDecision.model_validate(
        {
            **decision.model_dump(mode="python"),
            "decision": route,
            "score": score,
            "rationale": (
                decision.rationale
                + "；确定性报告检查发现："
                + "、".join(item.code for item in issues)
            )[:4000],
            "issues": merged,
            "revision_instructions": instructions,
        }
    )
