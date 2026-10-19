"""Citation-first evidence packets for quality review and local report repair."""

from __future__ import annotations

import re
from collections.abc import Sequence

from atlasflow.agents.contracts import QualityDecision, ReviewIssue
from atlasflow.agents.report_skill import build_citation_catalog, cited_numbers
from atlasflow.agents.report_structure import report_paragraphs
from atlasflow.schemas import Evidence, ToolCallRecord

REVIEW_EVIDENCE_CHAR_BUDGET = 60_000
REVIEW_SOURCE_CHAR_LIMIT = 4_000
_REFERENCE = re.compile(r"(?<![\\\[!])\[(\d{1,3})\](?!\s*[(:])")


def build_review_context(
    report: str,
    evidence: Sequence[Evidence],
    records: Sequence[ToolCallRecord],
    *,
    source_order: Sequence[str] = (),
    issues: Sequence[ReviewIssue] = (),
    preferred_source_urls: Sequence[str] = (),
    fallback: str = "",
) -> str:
    """Every cited known source gets a slot, regardless of the writing top-30.

    Bounds apply to excerpts, not identity. Empty/truncated slots are explicit:
    not seeing a passage does not establish that it does not exist. No model,
    search or database call is made here. Only report/task-related sources leave
    this function; no unrelated full-catalog dump is included.
    """
    catalog = build_citation_catalog(list(evidence), list(records), source_order=source_order)
    known = {item.number: item for item in catalog}
    required = cited_numbers(report)
    for issue in issues:
        required.update(issue.source_numbers)
        required.update(int(number) for number in _REFERENCE.findall(
            issue.message + "\n" + issue.recommendation
        ))
    selected = [item for item in catalog if item.number in required]
    preferred = set(preferred_source_urls)
    current_urls = {item.uri for item in evidence if "origin_run_id" not in item.metadata}
    alternatives = preferred or current_urls
    # Bound alternatives can correct a misplaced citation, but cannot evict a
    # source already cited. These URLs must come from current-task bindings.
    selected.extend([item for item in catalog
                     if item.number not in required and item.url in alternatives][:8])
    texts = {item.number: re.sub(r"\s+", " ", item.excerpt).strip() for item in selected}
    share = REVIEW_EVIDENCE_CHAR_BUDGET // max(len(selected), 1)
    sizes = {number: min(len(text), share, REVIEW_SOURCE_CHAR_LIMIT)
             for number, text in texts.items()}
    remaining = REVIEW_EVIDENCE_CHAR_BUDGET - sum(sizes.values())
    for number, text in texts.items():
        extra = min(max(min(len(text), REVIEW_SOURCE_CHAR_LIMIT) - sizes[number], 0), remaining)
        sizes[number] += extra
        remaining -= extra
    lines = [
        "审查引用证据包（编号沿用当前稿；不受写作摘录30条上限限制）：",
        "只能根据实际可见内容判断。未展示、截断或未读取全文，不等于来源不支持或事实错误。",
        "完整只表示已存片段完整展示，不表示读取过整个网页或完成事实交叉核验。",
    ]
    missing = sorted(required - known.keys())
    if missing:
        lines.append("不存在的引用编号：" + ", ".join(map(str, missing)))
    for item in selected:
        excerpt = texts[item.number][:sizes[item.number]]
        status = "片段为空" if not texts[item.number] else (
            "已存片段完整展示" if sizes[item.number] == len(texts[item.number]) else "已存片段被截断"
        )
        lines.extend([
            f"[{item.number}] {' '.join(item.title.split())}", f"URL: {item.url}",
            f"获取方式: {item.access}", f"可见范围: {status}（{sizes[item.number]}/{len(texts[item.number])}字符）",
            f"材料片段: {excerpt or '只有来源链接，未取得可核验的正文片段'}",
        ])
    lines.append("本包展示编号：" + ", ".join(str(item.number) for item in selected))
    if fallback and not selected:
        lines.extend(["", "其他安全工具摘要：", fallback[:min(remaining, 4_000)]])
    return "\n".join(lines)


def validate_review_locations(decision: QualityDecision, report: str) -> QualityDecision:
    """Reject invented anchors; unlocated historical issues stay readable."""
    known = {paragraph.id for paragraph in report_paragraphs(report)}
    for issue in decision.issues:
        if len(issue.paragraph_ids) != len(set(issue.paragraph_ids)):
            raise ValueError("quality paragraph_ids must be unique")
        if set(issue.paragraph_ids) - known:
            raise ValueError("quality paragraph_ids must refer to the current draft index")
        if len(issue.source_numbers) != len(set(issue.source_numbers)):
            raise ValueError("quality source_numbers must be unique")
        if issue.scope == "local" and not issue.paragraph_ids:
            raise ValueError("local quality issues require a paragraph_id from the current draft")
    return decision
