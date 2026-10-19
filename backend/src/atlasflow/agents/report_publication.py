"""Publish an existing complete manuscript with explicit verification boundaries.

Warnings do not erase a manuscript. Disputed passages and review notes belong
in the limitations section, not repeated banners throughout the report. No model
call or semantic truth classification is performed here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher

from atlasflow.agents.contracts import DraftVersion, ReportUpdateContext, ReviewIssue, Severity
from atlasflow.agents.report_delivery import _historical_body
from atlasflow.agents.report_skill import (
    build_citation_catalog,
    cited_numbers,
    render_academic_references,
    report_prose,
)
from atlasflow.agents.report_structure import (
    HEADING as _HEADING,
)
from atlasflow.agents.report_structure import (
    REFERENCES as _REFERENCES,
)
from atlasflow.agents.report_structure import (
    SECOND_HEADING as _SECOND_HEADING,
)
from atlasflow.agents.report_structure import (
    ReportParagraph,
    report_paragraphs,
)
from atlasflow.agents.report_structure import (
    paragraph_spans as _paragraph_spans,
)
from atlasflow.agents.report_structure import (
    report_parts as _report_parts,
)
from atlasflow.agents.task_quality import has_substantive_text
from atlasflow.schemas import Evidence, ToolCallRecord

_CITATION = re.compile(r"(?<![\\\[!])\[(\d{1,3})\](?!\s*[(:])")
_INTERNAL = re.compile(r"(?<![\\\[!])\[(?:S\d+|task:[^\]\n]+|[0-9a-fA-F]{8,36})\](?!\s*[(:])")
_QUOTED = re.compile(r"[“「]([^”」\n]{4,})[”」]|\"([^\"\n]{4,})\"")
_WEB_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_RAW_URL = re.compile(r"https?://[^\s<>\]\[）)]+")
# These identify missing material, not an assertion to quarantine. They remain
# visible issues and never turn the original quality rejection into acceptance.
_OMISSIONS = frozenset(
    {
        "missing_limitation",
        "report_update_missing_content",
        "report_missing_sections",
        "presentation_style",
        "presentation_completeness",
        "report_too_brief",
        "report_limitation_note",
        "report_optional_detail",
    }
)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _note(text: str) -> str:
    """Review comments are not new sources or bibliography citations."""
    text = _CITATION.sub(lambda match: f"引用{match.group(1)}", text)
    text = _INTERNAL.sub("内部引用标记", text)
    text = re.sub(r"https?://[^\s)\]<>]+", "来源链接", text)
    return re.sub(r"\s+", " ", text).strip()[:1800]


def _source_links(fragment: str, numbers: dict[str, int]) -> tuple[str, bool]:
    unknown = False
    for match in reversed(list(_WEB_LINK.finditer(report_prose(fragment)))):
        number = numbers.get(match.group(2))
        unknown |= number is None
        replacement = match.group(1) + (f"[{number}]" if number else "")
        fragment = fragment[: match.start()] + replacement + fragment[match.end() :]
    for match in reversed(list(_RAW_URL.finditer(report_prose(fragment)))):
        url, punctuation = match.group(0), ""
        while url not in numbers and url and url[-1] in ".,;:!?。，；：！？":
            url, punctuation = url[:-1], url[-1] + punctuation
        number = numbers.get(url)
        unknown |= number is None
        replacement = (f"[{number}]" if number else "") + punctuation
        fragment = fragment[: match.start()] + replacement + fragment[match.end() :]
    return fragment, unknown


def _clean_references(fragment: str, known: set[int], numbers: dict[str, int]) -> tuple[str, bool]:
    invalid = cited_numbers(fragment) - known
    matches = [
        match for match in _CITATION.finditer(report_prose(fragment))
        if int(match.group(1)) in invalid
    ]
    matches.extend(_INTERNAL.finditer(report_prose(fragment)))
    for match in sorted(matches, key=lambda item: item.start(), reverse=True):
        fragment = fragment[:match.start()] + fragment[match.end():]
    fragment, unknown_links = _source_links(fragment, numbers)
    return fragment, bool(matches) or unknown_links


def usable_manuscript(content: str) -> bool:
    """Headings/bibliography alone must not replace a report built from findings."""
    prose = report_prose(content)
    references = _REFERENCES.search(prose)
    body = prose[: references.start()] if references else prose
    return any(has_substantive_text(body[start:finish]) for start, finish in _paragraph_spans(body))


def _locator_text(text: str) -> str:
    """Normalise text for location only, never for deciding factual support."""
    text = _CITATION.sub("", report_prose(text))
    return "".join(character for character in text if character.isalnum()).casefold()


def locate_issue_paragraphs(issue: ReviewIssue, content: str) -> set[str]:
    paragraphs = report_paragraphs(content)
    known = {paragraph.id for paragraph in paragraphs}
    if issue.paragraph_ids:
        # Explicit draft-local anchors are authoritative; never silently remap
        # a bad/stale ID to some other paragraph based on a similar word.
        return set(issue.paragraph_ids) & known
    body = [paragraph for paragraph in paragraphs if not paragraph.in_limitations]
    quotes = [left or right for left, right in _QUOTED.findall(issue.message)]
    matches: set[str] = set()
    for quote in quotes:
        parts = [_locator_text(part) for part in re.split(r"…+|\.{3,}", quote)]
        parts = [part for part in parts if len(part) >= 6]
        if not parts:
            continue
        for paragraph in body:
            text = _locator_text(paragraph.text)
            if all(part in text for part in parts):
                matches.add(paragraph.id)
    if matches:
        return matches
    # Historical reviews sometimes paraphrase a word inside their quote. Permit
    # a tightly constrained, source-bound fuzzy locator for long quotes only.
    # This changes location, not whether a sentence is true or accepted.
    source_numbers = set(issue.source_numbers) | cited_numbers(issue.message)
    for quote in quotes:
        anchor = _locator_text(quote)
        if len(anchor) < 16 or not source_numbers:
            continue
        candidates: list[ReportParagraph] = []
        for paragraph in body:
            if not (cited_numbers(paragraph.text) & source_numbers):
                continue
            matcher = SequenceMatcher(
                None, anchor[:800], _locator_text(paragraph.text)[:12_000], autojunk=False,
            )
            blocks = matcher.get_matching_blocks()
            if sum(block.size for block in blocks) / len(anchor[:800]) >= 0.9 and max(
                (block.size for block in blocks), default=0,
            ) >= 12:
                candidates.append(paragraph)
        if len(candidates) == 1:
            matches.add(candidates[0].id)
    return matches


def consolidate_report_limitations(content: str, notes: Sequence[str] = ()) -> str:
    """Merge research gaps before review, so acceptance applies to the actual text.

    This is Markdown organisation, not a second quality verdict. Empty manuscripts
    stay empty rather than gaining a boilerplate paragraph that looks like research.
    """
    if not usable_manuscript(content):
        return content
    body, limitations, bibliography = _report_parts(content)
    for note in dict.fromkeys(_note(item) for item in notes if item.strip()):
        if note and _compact(note) not in _compact(limitations):
            limitations += ("\n\n" if limitations else "") + "- " + note
    if not limitations:
        limitations = "本报告依据本次已取得的材料整理，信息时效与适用范围以所引来源为准。"
    return body + "\n\n## 局限\n\n" + limitations + (
        "\n\n" + bibliography if bibliography else ""
    )


def build_warning_publication(
    draft: DraftVersion,
    evidence: Sequence[Evidence],
    records: Sequence[ToolCallRecord],
    *,
    reason: str,
    issues: Sequence[ReviewIssue] = (),
    warnings: Sequence[str] = (),
    version: int,
    withhold_conclusions: bool = False,
    report_update: ReportUpdateContext | None = None,
    missing_baseline_sections: Sequence[str] = (),
) -> DraftVersion:
    """Keep report scope, collecting disputed prose in the limitations section.

    Structured IDs locate current issues; quotes are a legacy fallback. A failed
    locator records an unresolved issue, never marks every paragraph as false.
    Unreviewed/core-blocked conclusions can be withheld without erasing evidence
    and analysis throughout the body. Delivery still is not factual acceptance.
    """
    catalog = build_citation_catalog(
        list(evidence), list(records), source_order=draft.citation_source_urls
    )
    known = {item.number for item in catalog}
    numbers = {item.url: item.number for item in catalog}
    if not usable_manuscript(draft.content):
        raise ValueError("cannot publish an empty manuscript")
    body, limitations, _ = _report_parts(draft.content)
    visible = report_prose(body)
    paragraphs = _paragraph_spans(visible)
    indexed = [paragraph for paragraph in report_paragraphs(draft.content)
               if not paragraph.in_limitations]
    indices = {paragraph.id: index for index, paragraph in enumerate(indexed)}
    marked: set[int] = set()
    restrict_conclusions = withhold_conclusions
    unlocated: list[ReviewIssue] = []
    for issue in issues:
        if issue.severity is Severity.INFO or issue.code in _OMISSIONS:
            continue
        locations = locate_issue_paragraphs(issue, draft.content)
        matches = {indices[location] for location in locations if location in indices}
        if matches:
            marked.update(matches)
        elif not locations:
            unlocated.append(issue)
        if issue.severity is Severity.CRITICAL and (
            issue.scope == "core" or (not locations and issue.scope == "unknown")
        ):
            restrict_conclusions = True
    if restrict_conclusions:
        for index, (start, _) in enumerate(paragraphs):
            headings = list(_HEADING.finditer(visible[:start]))
            if headings and re.fullmatch(r"##[ \t]+(?:摘要|结论)[ \t]*", headings[-1].group(0)):
                marked.add(index)

    # Remove invalid references without inventing replacements. Affected text is
    # preserved as material under limitations, not as a confirmed body assertion.
    edits: list[tuple[int, int, str]] = []
    pending: list[tuple[str, str]] = []
    for index, (start, finish) in enumerate(paragraphs):
        fragment, invalid_references = _clean_references(body[start:finish], known, numbers)
        if invalid_references:
            marked.add(index)
        if index in marked:
            headings = list(_HEADING.finditer(visible[:start]))
            title = re.sub(r"^#+[ \t]+", "", headings[-1].group(0)) if headings else "正文"
            pending.append((_note(title), fragment))
            fragment = ""
        edits.append((start, finish, fragment))
    for start, finish, replacement in reversed(edits):
        body = body[:start] + replacement + body[finish:]
    limitations, _ = _clean_references(limitations, known, numbers)
    # Keep emptied sections understandable, without repeating warning banners.
    headings = list(_SECOND_HEADING.finditer(report_prose(body)))
    for index in reversed(range(len(headings))):
        heading = headings[index]
        finish = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        if not has_substantive_text(report_prose(body[heading.end():finish])):
            body = (
                body[:heading.end()]
                + "\n\n本节相关资料见文末局限，本次不据此形成确定结论。\n\n"
                + body[finish:]
            )
    if report_update and missing_baseline_sections:
        missing = set(missing_baseline_sections)
        sections = [
            section.content for section in report_update.sections if section.section_id in missing
        ]
        if sections:
            historical = _historical_body(
                report_update.model_copy(update={"base_report": "\n\n".join(sections)}),
                numbers,
            )
            limitations += (
                "\n\n### 来源报告补充（历史基线）\n\n"
                "以下为本轮仍遗漏的旧稿材料，仅作历史对照，未按变更后的预算、日期等条件重新验收，不是本轮已确认结论。\n\n"
                + "\n".join("> " + line if line else ">" for line in historical.splitlines())
            )
    if pending:
        limitations += "\n\n### 待核实事项\n\n以下保留原稿相关材料，不作为已确认事实或出行承诺。\n\n"
        limitations += "\n\n".join(
            f"**{index}. {title}**\n\n"
            + "\n".join("> " + line if line else ">" for line in fragment.splitlines())
            for index, (title, fragment) in enumerate(pending, 1)
        )
    notes = list(dict.fromkeys(_note(item) for item in [reason, *warnings] if item.strip()))
    notes.extend(
        f"{'重点核验' if issue.severity is Severity.CRITICAL else '核验提示'}：{_note(issue.message)}"
        + (f" 建议：{_note(issue.recommendation)}" if issue.recommendation else "")
        for issue in issues
    )
    if unlocated:
        notes.insert(
            0, f"有{len(unlocated)}项审查问题未能定位到具体段落，已保留其原始说明；不因此判定其余正文均无效。"
        )
    if restrict_conclusions:
        notes.insert(0, "本次审查未完成或核心结论尚有阻断，摘要与结论不作确定性承诺；正文仅保留已有材料与分析，不表示全部已核实。")
    notes.insert(0, "本报告已生成并可下载，未获得完整质量验收；信息使用范围以本节说明为准。")
    content = (
        body.rstrip() + "\n\n## 局限\n\n" + limitations.strip()
        + "\n\n### 核验说明\n\n" + "\n\n".join("- " + note for note in notes)
    )
    return DraftVersion(
        version=version,
        plan_version=draft.plan_version,
        content=render_academic_references(
            content, list(evidence), list(records), source_order=draft.citation_source_urls
        ),
        based_on_task_ids=list(draft.based_on_task_ids),
        citation_source_urls=[item.url for item in catalog],
    )
