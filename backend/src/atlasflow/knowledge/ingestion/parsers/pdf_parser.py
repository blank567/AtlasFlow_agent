from __future__ import annotations

import re
from io import BytesIO

from atlasflow.knowledge.domain import ParsedBlock
from atlasflow.knowledge.ingestion.parsers.common import normalize_extracted_text

_NAMED_SECTIONS = {
    "abstract", "summary", "introduction", "background", "related work",
    "methods", "methodology", "experiments", "results", "discussion",
    "conclusion", "conclusions", "acknowledgements", "acknowledgments",
    "references", "bibliography", "appendix",
}
_NUMBERED_SECTION = re.compile(r"^(?P<number>\d+(?:\.\d+){0,3})\.?\s+(?P<title>[^\n]+)$")


def _section_heading(line: str, *, in_references: bool) -> tuple[int, str, str] | None:
    """Only recognize conservative, standalone headings from plain PDF text."""
    label = line.strip()
    normalized = label.rstrip(".:").casefold()
    if normalized in _NAMED_SECTIONS:
        role = "references" if normalized in {"references", "bibliography"} else (
            "abstract" if normalized in {"abstract", "summary"} else "body"
        )
        return 1, label, role
    # Reference entries often start with a number; do not mistake them for sections.
    if in_references or len(label) > 100 or label.endswith((".", ",", ";", ":")):
        return None
    match = _NUMBERED_SECTION.fullmatch(label)
    if match is None:
        return None
    title = match.group("title").strip()
    if len(title.split()) > 12 or not title[:1].isalpha():
        return None
    return len(match.group("number").split(".")), label, "body"


def _paragraph_block(
    text: str,
    start: int,
    end: int,
    *,
    page_number: int,
    order: int,
    heading_path: list[str],
    section_role: str,
) -> ParsedBlock:
    segment = text[start:end]
    trimmed_start = start + len(segment) - len(segment.lstrip())
    trimmed_end = start + len(segment.rstrip())
    return ParsedBlock(
        block_type="paragraph",
        text=text[trimmed_start:trimmed_end],
        heading_path=heading_path,
        page_number=page_number,
        order=order,
        source_locator={
            "page_number": page_number,
            "source_char_start": trimmed_start,
            "source_char_end": trimmed_end,
            "section_role": section_role,
        },
    )


class PdfDocumentParser:
    mime_types = ("application/pdf",)

    def parse(self, content: bytes) -> list[ParsedBlock]:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        blocks: list[ParsedBlock] = []
        hierarchy: list[tuple[int, str]] = []
        section_role = "body"
        for page_number, page in enumerate(reader.pages, 1):
            text = normalize_extracted_text(page.extract_text() or "").strip()
            paragraph_start: int | None = None
            paragraph_end = 0

            for match in re.finditer(r"[^\n]+", text):
                line = match.group().strip()
                if not line:
                    continue
                heading = _section_heading(line, in_references=section_role == "references")
                if heading is not None:
                    if paragraph_start is not None:
                        blocks.append(_paragraph_block(
                            text, paragraph_start, paragraph_end, page_number=page_number,
                            order=len(blocks), heading_path=[title for _, title in hierarchy],
                            section_role=section_role,
                        ))
                        paragraph_start = None
                    level, title, section_role = heading
                    while hierarchy and hierarchy[-1][0] >= level:
                        hierarchy.pop()
                    hierarchy.append((level, title))
                    blocks.append(
                        ParsedBlock(
                            block_type="heading",
                            text=title,
                            heading_path=[item for _, item in hierarchy],
                            page_number=page_number,
                            order=len(blocks),
                            source_locator={
                                "page_number": page_number,
                                "source_char_start": match.start(),
                                "source_char_end": match.end(),
                                "section_role": section_role,
                            },
                        )
                    )
                    continue
                if paragraph_start is not None and text[paragraph_end:match.start()].count("\n") > 1:
                    blocks.append(_paragraph_block(
                        text, paragraph_start, paragraph_end, page_number=page_number,
                        order=len(blocks), heading_path=[title for _, title in hierarchy],
                        section_role=section_role,
                    ))
                    paragraph_start = None
                if paragraph_start is None:
                    paragraph_start = match.start()
                paragraph_end = match.end()
            if paragraph_start is not None:
                blocks.append(_paragraph_block(
                    text, paragraph_start, paragraph_end, page_number=page_number,
                    order=len(blocks), heading_path=[title for _, title in hierarchy],
                    section_role=section_role,
                ))
        return blocks
