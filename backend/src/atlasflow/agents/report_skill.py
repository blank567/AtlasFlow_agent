"""Project-local academic report skill and deterministic citation rendering."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from atlasflow.agents.web_evidence import evidence_access_label, evidence_access_priority
from atlasflow.schemas import Evidence, ToolCallRecord

_SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "academic-report"
_FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_REFERENCE_HEADING = re.compile(r"^##\s+(?:来源|参考文献)\s*$", re.MULTILINE)
_NEXT_HEADING = re.compile(r"^##\s+", re.MULTILINE)
_NUMERIC_CITATION = re.compile(r"(?<![\\\[!])\[(\d{1,3})\](?!\s*[(:])")
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)[\s\S]*?\1(?!`)")
_LINK_SAFE_CHARS = "/:#?&=@%+;,"
_REPORT_EXCERPT_LIMIT = 30


@dataclass(frozen=True, slots=True)
class ReportCitation:
    number: int
    title: str
    url: str
    excerpt: str
    kind: str = "web"
    access: str = "获取方式未标注"


@lru_cache(maxsize=1)
def academic_report_instructions() -> str:
    """Load the versioned skill and citation contract used by Synthesizer."""

    skill = _FRONTMATTER.sub("", (_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"))
    citation = (_SKILL_ROOT / "references" / "citation-contract.md").read_text(
        encoding="utf-8"
    )
    return f"\n\n以下是必须遵守的项目写作 Skill：\n{skill}\n\n{citation}\n"


def build_citation_catalog(
    evidence: list[Evidence] | tuple[Evidence, ...],
    records: list[ToolCallRecord] | tuple[ToolCallRecord, ...],
    *,
    source_order: Sequence[str] = (),
) -> list[ReportCitation]:
    """Assign stable numbers to unique, persisted source and navigation URLs."""

    rows: list[tuple[str, str, str, str, str]] = []
    seen: set[str] = set()
    positions: dict[str, int] = {}
    priorities: dict[str, int] = {}
    for item in evidence:
        url = item.uri or ""
        if not url.startswith(("https://", "http://")):
            continue
        priority = evidence_access_priority(item.metadata)
        row = (item.title.strip() or "未命名网页", url, item.content.strip(), "web",
               evidence_access_label(item.metadata))
        if url in seen:
            if priority > priorities[url]:
                # Upgrade this excerpt in place. Never change the URL's number
                # when on-demand reading follows a search for the same page.
                rows[positions[url]] = row
                priorities[url] = priority
            continue
        seen.add(url)
        positions[url] = len(rows)
        priorities[url] = priority
        rows.append(row)
    navigation_index = 0
    for record in records:
        for index, url in enumerate(record.navigation_urls or (
            [record.navigation_url] if record.navigation_url else []
        )):
            if not url.startswith(("https://", "http://")) or url in seen:
                continue
            seen.add(url)
            navigation_index += 1
            leg = record.navigation_leg_indices[index] if index < len(record.navigation_leg_indices) else navigation_index
            rows.append(
                (
                    f"高德地图导航（第 {leg} 段）",
                    url,
                    (record.summary or "打开高德地图核对正式地点、最新路线、距离、耗时与交通状况。")
                    + ("本次工具未完整成功，仅该链接对应路段已核验。" if not record.success else ""),
                    "navigation",
                    "地图工具返回的导航链接（按记录保留核验范围）",
                )
            )
    if source_order:
        order = {url: index for index, url in enumerate(dict.fromkeys(source_order))}
        rows.sort(key=lambda row: order.get(row[1], len(order)))
    return [
        ReportCitation(number=index, title=title, url=url, excerpt=excerpt, kind=kind, access=access)
        for index, (title, url, excerpt, kind, access) in enumerate(rows, 1)
    ]


def build_report_context(
    evidence: list[Evidence] | tuple[Evidence, ...],
    records: list[ToolCallRecord] | tuple[ToolCallRecord, ...],
    *,
    fallback: str = "",
    source_order: Sequence[str] = (),
    preferred_source_urls: Sequence[str] = (),
    cited_report: str = "",
) -> str:
    """Keep every source identity visible and bound excerpts without old-source starvation."""

    catalog = build_citation_catalog(evidence, records, source_order=source_order)
    if not catalog:
        return fallback
    lines = [
        "引用目录（系统生成，编号固定；只能引用与论断语义相符的条目）："
    ]
    selected = _select_report_excerpts(
        catalog, evidence, preferred_source_urls=preferred_source_urls, cited_report=cited_report
    )
    if len(catalog) > _REPORT_EXCERPT_LIMIT:
        lines.extend([
            "完整来源索引（保留所有已知编号与链接；仅列入索引不表示已有支持论断的正文片段）：",
            *(
                f"[{item.number}] {' '.join(item.title.split())[:200]} | URL: {item.url}"
                for item in catalog
            ),
            "本次材料片段（编号沿用完整索引，可以不连续；未展示的片段不等于来源不存在）：",
        ])
    for item in selected:
        excerpt = re.sub(r"\s+", " ", item.excerpt)[:900]
        lines.extend(
            [
                f"[{item.number}] {item.title}",
                f"URL: {item.url}",
                f"获取方式: {item.access}",
                f"材料片段: {excerpt or '仅取得可核查链接，未提供正文摘录'}",
            ]
        )
    if len(catalog) > _REPORT_EXCERPT_LIMIT:
        lines.append(
            "片段展示优先兼顾既有正文引用与本轮相关来源；该偏好不代表真实性或必须引用。"
            "仅有索引链接的来源不得用于推断未取得的新事实；不得自行追加或重排编号。"
        )
    if fallback:
        lines.extend(["", "其他安全工具摘要：", fallback])
    return "\n".join(lines)


def _select_report_excerpts(
    catalog: list[ReportCitation],
    evidence: list[Evidence] | tuple[Evidence, ...],
    *,
    preferred_source_urls: Sequence[str],
    cited_report: str,
) -> list[ReportCitation]:
    """Share the excerpt budget between cited context and current relevant evidence.

    Catalog numbers and same-URL excerpt upgrades are left untouched. An origin
    tag marks inherited evidence even when its value is empty; only untagged
    evidence is eligible for the current-run fallback priority.
    """

    if len(catalog) <= _REPORT_EXCERPT_LIMIT:
        return catalog
    by_url = {item.url: item for item in catalog}
    current_urls = {
        item.uri for item in evidence if "origin_run_id" not in item.metadata
    }
    current = [item for item in catalog if item.url in current_urls]
    used_numbers = cited_numbers(cited_report)
    cited = [item for item in catalog if item.number in used_numbers]
    preferred = [
        by_url[url] for url in dict.fromkeys(preferred_source_urls) if url in by_url
    ]
    # A preferred list can itself contain a long inherited prefix. Prioritize
    # its fresh evidence without ever interpreting retrieval order as truth.
    cited.sort(key=lambda item: item.url not in current_urls)
    preferred.sort(key=lambda item: item.url not in current_urls)
    groups = [iter(cited), iter(preferred or current)]
    selected: dict[str, ReportCitation] = {}
    while groups and len(selected) < _REPORT_EXCERPT_LIMIT:
        remaining = []
        for group in groups:
            candidate = next(group, None)
            while candidate is not None and candidate.url in selected:
                candidate = next(group, None)
            if candidate is not None:
                selected[candidate.url] = candidate
                remaining.append(group)
            if len(selected) == _REPORT_EXCERPT_LIMIT:
                break
        groups = remaining
    for item in [*current, *catalog]:
        if len(selected) == _REPORT_EXCERPT_LIMIT:
            break
        selected.setdefault(item.url, item)
    return sorted(selected.values(), key=lambda item: item.number)


def report_prose(report: str) -> str:
    """Mask code without shifting offsets, so citation/render/audit use one view."""

    def mask(text: str) -> str:
        return re.sub(r"[^\r\n]", " ", text)

    lines: list[str] = []
    fence: tuple[str, int] | None = None
    for line in report.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)", line)
        if fence is not None:
            if (marker and marker.group(1)[0] == fence[0]
                    and len(marker.group(1)) >= fence[1] and not marker.group(2).strip()):
                fence = None
            lines.append(mask(line))
        elif marker:
            fence = (marker.group(1)[0], len(marker.group(1)))
            lines.append(mask(line))
        else:
            lines.append(line)
    return _INLINE_CODE.sub(lambda match: mask(match.group(0)), "".join(lines))


def cited_numbers(report: str) -> set[int]:
    """Return citation numbers used before the bibliography section."""

    prose = report_prose(report)
    section = _REFERENCE_HEADING.search(prose)
    body = prose[: section.start()] if section else prose
    return {int(value) for value in _NUMERIC_CITATION.findall(body)}


def render_academic_references(
    report: str,
    evidence: list[Evidence] | tuple[Evidence, ...],
    records: list[ToolCallRecord] | tuple[ToolCallRecord, ...],
    *,
    source_order: Sequence[str] = (),
) -> str:
    """Replace model-written bibliography text with cited, verified catalog entries."""

    catalog = {
        item.number: item
        for item in build_citation_catalog(evidence, records, source_order=source_order)
    }
    used = sorted(number for number in cited_numbers(report) if number in catalog)
    entries = []
    for number in used:
        item = catalog[number]
        title = re.sub(r"[\[\]\r\n<>]", " ", item.title).strip()[:200]
        url = quote(item.url, safe=_LINK_SAFE_CHARS)
        medium = "[地图/OL]" if item.kind == "navigation" else "[EB/OL]"
        entries.append(f"[{number}] {title}{medium}. [访问链接]({url})")
    if not entries and not catalog:
        entries = ["本报告基于题目给定信息与直接计算，不引用外部资料。"]
    elif not entries:
        entries = ["尚未在正文中建立有效的编号引用；请在修订时补充。"]

    replacement = "## 参考文献\n\n" + "\n\n".join(entries)
    prose = report_prose(report)
    section = _REFERENCE_HEADING.search(prose)
    if section is None:
        return report.rstrip() + "\n\n" + replacement
    next_section = _NEXT_HEADING.search(prose, section.end())
    end = next_section.start() if next_section else len(report)
    return report[: section.start()].rstrip() + "\n\n" + replacement + report[end:]
