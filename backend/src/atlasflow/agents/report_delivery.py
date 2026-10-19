"""Model-independent, explicitly limited reports for recoverable provider failures."""

from __future__ import annotations

import re
from collections.abc import Sequence

from atlasflow.agents.contracts import (
    DraftVersion,
    ReportUpdateContext,
    ResearchPlan,
    ResearchResult,
    ReviewIssue,
    Severity,
)
from atlasflow.agents.report_skill import (
    build_citation_catalog,
    render_academic_references,
    report_prose,
)
from atlasflow.agents.task_quality import validate_task_result
from atlasflow.schemas import Evidence, ToolCallRecord

_LINK = re.compile(r"\[([^\]\n]+)\]\(https?://[^\s)]+\)")
_URL = re.compile(r"https?://[^\s<>\]\[）)]+")
_CITATION = re.compile(r"\[(?:\d{1,3}|S\d+|task:[^\]\n]+)\]")
_HISTORICAL_NUMBER = re.compile(r"(?<![\\\[!])\[(\d{1,3})\](?!\s*[(:])")


def _plain(text: str) -> str:
    """Do not carry model-written links or citation numbers into a new catalog."""

    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _CITATION.sub("", text)
    return re.sub(r"\s+", " ", text).strip()[:1800]


def _historical_body(context: ReportUpdateContext, numbers: dict[str, int]) -> str:
    """Translate historical citation identities; never reuse an unrelated number."""
    body = context.base_report
    prose = report_prose(body)
    boundary = re.search(r"^##\s+(?:参考文献|来源)\s*$", prose, re.MULTILINE)
    if boundary:
        body, prose = body[:boundary.start()], prose[:boundary.start()]
    for match in reversed(list(_HISTORICAL_NUMBER.finditer(prose))):
        old_index = int(match.group(1)) - 1
        url = context.citation_source_urls[old_index] if 0 <= old_index < len(context.citation_source_urls) else ""
        replacement = f"[{numbers[url]}]" if url in numbers else ""
        body = body[:match.start()] + replacement + body[match.end():]
    # Historical drafts can predate the numbered-citation contract. Strip link
    # wrappers and only retain raw URLs as numbered references to known sources.
    for match in reversed(list(_LINK.finditer(report_prose(body)))):
        link = _URL.search(match.group(0))
        number = numbers.get(link.group(0)) if link else None
        replacement = match.group(1) + (f"[{number}]" if number else "")
        body = body[:match.start()] + replacement + body[match.end():]
    for match in reversed(list(_URL.finditer(report_prose(body)))):
        url, punctuation = match.group(0), ""
        while url not in numbers and url and url[-1] in ".,;:!?。，；：！？":
            url, punctuation = url[:-1], url[-1] + punctuation
        replacement = (f"[{numbers[url]}]" if url in numbers else "") + punctuation
        body = body[:match.start()] + replacement + body[match.end():]
    return body.rstrip()


def build_delivery_report(
    query: str,
    plan: ResearchPlan | None,
    results: Sequence[ResearchResult],
    evidence: Sequence[Evidence],
    records: Sequence[ToolCallRecord],
    *,
    reason: str,
    version: int = 1,
    source_order: Sequence[str] = (),
    issues: Sequence[ReviewIssue] = (),
    withhold_conclusions: bool = False,
    report_update: ReportUpdateContext | None = None,
) -> DraftVersion:
    """Deliver available findings, never a fabricated answer or quality verdict.

    A failed provider is not replaced by another hidden model call. Fresh-data
    findings still need an actual acquired source. Known critical findings are
    withheld; an unscoped critical issue conservatively withholds all derived
    conclusions rather than republishing a rejected draft as a final answer.
    """

    if report_update and not source_order:
        source_order = report_update.citation_source_urls
    sources, calls = list(evidence), list(records)
    catalog = build_citation_catalog(sources, calls, source_order=source_order)
    numbers = {item.url: item.number for item in catalog}
    anchors = {item.id: {numbers[item.uri]} for item in sources if item.uri in numbers}
    for call in calls:
        bound = set().union(*(anchors.get(key, set()) for key in call.evidence_ids))
        if not call.success:
            bound = set()
        bound.update(
            numbers[url]
            for url in call.navigation_urls
            or ([call.navigation_url] if call.navigation_url else [])
            if url in numbers
        )
        if bound:
            anchors[call.call_id] = bound
    critical = [item for item in issues if item.severity is Severity.CRITICAL]
    blocked = {item.task_id for item in critical}
    tasks = {item.task_id: item for item in plan.tasks} if plan else {}
    sections: list[str] = []
    included: list[str] = []
    limitations: list[str] = []
    for result in results:
        task = tasks.get(result.task_id)
        if task is None or result.plan_version != plan.plan_version:
            continue
        limitations.extend(result.limitations)
        if withhold_conclusions or None in blocked or result.task_id in blocked:
            continue
        try:
            validate_task_result(task, result)
        except ValueError:
            continue
        supported: dict[int, set[int]] = {}
        for assessment in result.assessments:
            if assessment.status not in {"met", "partial"}:
                continue
            refs = set().union(*(anchors.get(key, set()) for key in assessment.source_ids))
            for index in assessment.finding_indices:
                line = result.findings[index - 1]
                line_refs = refs | {number for url, number in numbers.items() if url in line}
                if not task.requires_fresh_data or line_refs:
                    supported.setdefault(index, set()).update(line_refs)
        if not result.assessments and not task.requires_fresh_data:
            supported = {index: set() for index in range(1, len(result.findings) + 1)}
        findings = []
        for index, refs in sorted(supported.items())[:8]:
            line = _plain(result.findings[index - 1])
            if line and any(char.isalnum() for char in line):
                findings.append("- " + line + "".join(f"[{number}]" for number in sorted(refs)))
        if findings:
            included.append(result.task_id)
            sections.append(f"### {_plain(task.title)}\n\n" + "\n\n".join(findings))
    missing = [item.title for item in tasks.values() if item.task_id not in included]
    if missing:
        limitations.append("以下部分尚未形成可交付的核验结论：" + "、".join(missing))
    limitations.extend(item.message for item in issues)
    if not sections:
        sections.append(
            "本次未取得足以支持确定结论的研究结果，不能据此编造当前数据、营业状态或导航路线。"
        )
        # These are labelled source excerpts, not an assertion that the query
        # was answered or that a contradictory research conclusion passed.
        excerpts = [
            f"- {_plain(item.title)}：{_plain(item.excerpt)[:600]}[{item.number}]"
            for item in catalog[:6]
            if item.excerpt and item.kind != "navigation"
        ]
        if excerpts:
            sections.append("### 已获取资料摘录（尚未综合核验）\n\n" + "\n\n".join(excerpts))
    limitations = list(dict.fromkeys(_plain(item) for item in limitations if item.strip()))[:20]
    limitations.insert(0, reason)
    historical = ""
    if report_update:
        limitations.extend(report_update.base_warnings)
        if withhold_conclusions or critical:
            limitations.append("来源报告正文未重新发布：当前存在未解决的关键核验问题，应回到历史报告查看原始版本")
        else:
            base = _historical_body(report_update, numbers)
            historical = (
                "\n\n## 来源报告历史基线\n\n"
                "以下保留来源报告原有内容，仅供历史对照；未按本轮变更需求重新验收，"
                "其动态事实不代表当前状态，不表示新增要求已完成。"
                "旧引用按来源URL对齐，无法对应已取得来源的编号和链接不保留。\n\n" + base
            )
    content = (
        "# 研究报告\n\n## 摘要\n\n"
        f"研究主题：{_plain(query)}\n\n"
        "本报告整理本次已经取得的材料，仅呈现可保留的研究发现；"
        "属于有限范围交付，未获得完整质量验收，不将未核实部分写成确定事实。\n\n"
        "## 研究发现\n\n"
        + "\n\n".join(sections)
        + historical
        + "\n\n## 结论\n\n"
        + (
            "本次可保留的结论见上述研究发现；未覆盖的事项不视为已核实。"
            if included
            else "现有材料不足以形成可靠的最终结论，本报告保留已获取资料及具体缺口。"
        )
        + "\n\n## 局限\n\n"
        + "\n\n".join("- " + item for item in limitations)
    )
    return DraftVersion(
        version=version,
        plan_version=plan.plan_version if plan else 1,
        content=render_academic_references(content, sources, calls, source_order=source_order),
        based_on_task_ids=included,
        citation_source_urls=[item.url for item in catalog],
    )
