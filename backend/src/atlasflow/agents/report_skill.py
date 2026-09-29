"""Project-local academic report skill and deterministic citation rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from atlasflow.schemas import Evidence, ToolCallRecord

_SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "academic-report"
_FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_REFERENCE_HEADING = re.compile(r"^##\s+(?:来源|参考文献)\s*$", re.MULTILINE)
_NEXT_HEADING = re.compile(r"^##\s+", re.MULTILINE)
_NUMERIC_CITATION = re.compile(r"(?<![\w[])\[(\d{1,3})\](?!\()")
_LINK_SAFE_CHARS = "/:#?&=@%+;,"


@dataclass(frozen=True, slots=True)
class ReportCitation:
    number: int
    title: str
    url: str
    excerpt: str
    kind: str = "web"


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
) -> list[ReportCitation]:
    """Assign stable numbers to unique, persisted source and navigation URLs."""

    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for item in evidence:
        url = item.uri or ""
        if not url.startswith(("https://", "http://")) or url in seen:
            continue
        seen.add(url)
        rows.append((item.title.strip() or "未命名网页", url, item.content.strip(), "web"))
    navigation_index = 0
    for record in records:
        for url in record.navigation_urls or (
            [record.navigation_url] if record.navigation_url else []
        ):
            if not url.startswith(("https://", "http://")) or url in seen:
                continue
            seen.add(url)
            navigation_index += 1
            rows.append(
                (
                    f"高德地图导航（第 {navigation_index} 段）",
                    url,
                    "打开高德地图核对正式地点、最新路线、距离、耗时与交通状况。",
                    "navigation",
                )
            )
    return [
        ReportCitation(number=index, title=title, url=url, excerpt=excerpt, kind=kind)
        for index, (title, url, excerpt, kind) in enumerate(rows, 1)
    ]


def build_report_context(
    evidence: list[Evidence] | tuple[Evidence, ...],
    records: list[ToolCallRecord] | tuple[ToolCallRecord, ...],
    *,
    fallback: str = "",
) -> str:
    """Give the writer a complete bounded catalog instead of a truncated source tail."""

    catalog = build_citation_catalog(evidence, records)
    if not catalog:
        return fallback
    lines = [
        "引用目录（系统生成，编号固定；只能引用与论断语义相符的条目）："
    ]
    for item in catalog[:30]:
        excerpt = re.sub(r"\s+", " ", item.excerpt)[:900]
        lines.extend(
            [
                f"[{item.number}] {item.title}",
                f"URL: {item.url}",
                f"可核验证据摘录: {excerpt or '仅取得可核查链接，未提供正文摘录'}",
            ]
        )
    if len(catalog) > 30:
        lines.append("其余来源未进入本次写作目录，不得自行追加编号。")
    if fallback:
        lines.extend(["", "其他安全工具摘要：", fallback])
    return "\n".join(lines)


def cited_numbers(report: str) -> set[int]:
    """Return citation numbers used before the bibliography section."""

    section = _REFERENCE_HEADING.search(report)
    body = report[: section.start()] if section else report
    return {int(value) for value in _NUMERIC_CITATION.findall(body)}


def render_academic_references(
    report: str,
    evidence: list[Evidence] | tuple[Evidence, ...],
    records: list[ToolCallRecord] | tuple[ToolCallRecord, ...],
) -> str:
    """Replace model-written bibliography text with cited, verified catalog entries."""

    catalog = {item.number: item for item in build_citation_catalog(evidence, records)}
    used = sorted(number for number in cited_numbers(report) if number in catalog)
    entries = []
    for number in used:
        item = catalog[number]
        title = re.sub(r"[\[\]\r\n<>]", " ", item.title).strip()[:200]
        url = quote(item.url, safe=_LINK_SAFE_CHARS)
        medium = "[地图/OL]" if item.kind == "navigation" else "[EB/OL]"
        entries.append(f"[{number}] {title}{medium}. {url}")
    if not entries and not catalog:
        entries = ["本报告基于题目给定信息与直接计算，不引用外部资料。"]
    elif not entries:
        entries = ["尚未在正文中建立有效的编号引用；请在修订时补充。"]

    replacement = "## 参考文献\n\n" + "\n\n".join(entries)
    section = _REFERENCE_HEADING.search(report)
    if section is None:
        return report.rstrip() + "\n\n" + replacement
    next_section = _NEXT_HEADING.search(report, section.end())
    end = next_section.start() if next_section else len(report)
    return report[: section.start()].rstrip() + "\n\n" + replacement + report[end:]
