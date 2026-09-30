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
from atlasflow.agents.report_skill import build_citation_catalog, cited_numbers
from atlasflow.schemas import Evidence, ToolCallRecord

_TASK_REFERENCE = re.compile(r"\[task:([A-Za-z0-9][A-Za-z0-9_.-]*)\]")
_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")
_REFERENCE_ENTRY = re.compile(r"^\[(\d{1,3})\]\s+", re.MULTILINE)
_REFERENCE_SECTION = re.compile(r"^##\s+(?:来源|参考文献)\s*$", re.MULTILINE)
_REQUIRED_HEADINGS = {"摘要", "结论", "局限", "参考文献"}
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

    catalog = build_citation_catalog(list(evidence), list(records))
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
