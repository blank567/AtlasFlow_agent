"""Stable draft-local Markdown locations shared by review and publication.

IDs locate existing text; they are not evidence or semantic truth judgments.
They are recomputed for each draft, never inserted into the reader's report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from atlasflow.agents.report_skill import report_prose

REFERENCES = re.compile(r"^##\s+(?:参考文献|来源)\s*$", re.MULTILINE)
HEADING = re.compile(r"^#{1,6}[ \t]+.+$", re.MULTILINE)
SECOND_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
LIMITATION_TITLES = {"局限", "局限与待核实事项", "局限与不确定性"}


def paragraph_spans(prose: str) -> list[tuple[int, int]]:
    paragraphs: list[tuple[int, int]] = []
    offset, start, end = 0, None, 0
    for line in prose.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        if not text.strip() or HEADING.fullmatch(text.strip()):
            if start is not None:
                paragraphs.append((start, end))
                start = None
        else:
            if start is None:
                start = offset
            end = offset + len(text)
        offset += len(line)
    if start is not None:
        paragraphs.append((start, end))
    return paragraphs


def report_parts(content: str) -> tuple[str, str, str]:
    prose = report_prose(content)
    references = REFERENCES.search(prose)
    end = references.start() if references else len(content)
    body, bibliography = content[:end].rstrip(), content[end:].strip()
    headings = list(SECOND_HEADING.finditer(prose[:end]))
    limits: list[str] = []
    edits: list[tuple[int, int]] = []
    for index, heading in enumerate(headings):
        if heading.group(1).strip() not in LIMITATION_TITLES:
            continue
        finish = headings[index + 1].start() if index + 1 < len(headings) else end
        limits.append(content[heading.end():finish].strip())
        edits.append((heading.start(), finish))
    for start, finish in reversed(edits):
        body = body[:start] + body[finish:]
    return body.rstrip(), "\n\n".join(dict.fromkeys(item for item in limits if item)), bibliography


@dataclass(frozen=True, slots=True)
class ReportParagraph:
    id: str
    section: str
    text: str
    start: int
    end: int
    in_limitations: bool


def report_paragraphs(content: str) -> list[ReportParagraph]:
    body, limitations, _ = report_parts(content)
    paragraphs: list[ReportParagraph] = []
    for prefix, part in (("P", body), ("L", limitations)):
        visible = report_prose(part)
        for index, (start, end) in enumerate(paragraph_spans(visible), 1):
            headings = list(HEADING.finditer(visible[:start]))
            title = re.sub(r"^#+[ \t]+", "", headings[-1].group()) if headings else (
                "局限" if prefix == "L" else "正文"
            )
            paragraphs.append(ReportParagraph(
                id=f"{prefix}{index:04d}", section=title, text=part[start:end],
                start=start, end=end, in_limitations=prefix == "L",
            ))
    return paragraphs


def paragraph_index(content: str) -> str:
    lines = ["段落定位索引（只用于本稿审查；P为正文，L为已声明局限，不是来源引用）："]
    for paragraph in report_paragraphs(content):
        anchor = " ".join(report_prose(paragraph.text).split())
        if len(anchor) > 260:
            anchor = anchor[:180] + " … " + anchor[-70:]
        lines.append(f"{paragraph.id} | {paragraph.section} | {anchor}")
    return "\n".join(lines)
