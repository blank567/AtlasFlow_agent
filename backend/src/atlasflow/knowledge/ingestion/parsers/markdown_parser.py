from __future__ import annotations

import re

from atlasflow.knowledge.domain import ParsedBlock
from atlasflow.knowledge.ingestion.parsers.common import normalize_extracted_text


class MarkdownParser:
    mime_types = ("text/markdown", "text/x-markdown")

    def parse(self, content: bytes) -> list[ParsedBlock]:
        lines = normalize_extracted_text(content.decode("utf-8-sig")).splitlines()
        blocks: list[ParsedBlock] = []
        headings: list[str] = []
        buffer: list[str] = []
        block_type = "paragraph"
        in_code = False
        buffer_start = 0
        buffer_end = 0

        def flush() -> None:
            nonlocal buffer, block_type, buffer_start, buffer_end
            text = "\n".join(buffer).strip()
            if text:
                blocks.append(
                    ParsedBlock(
                        block_type=block_type,  # type: ignore[arg-type]
                        text=text,
                        heading_path=list(headings),
                        order=len(blocks),
                        source_locator={"line_start": buffer_start, "line_end": buffer_end},
                    )
                )
            buffer = []
            block_type = "paragraph"
            buffer_start = 0
            buffer_end = 0

        def append(line: str, line_number: int) -> None:
            nonlocal buffer_start, buffer_end
            if not buffer:
                buffer_start = line_number
            buffer_end = line_number
            buffer.append(line)

        for line_number, line in enumerate(lines, 1):
            if line.startswith("```"):
                if in_code:
                    append(line, line_number)
                    flush()
                    in_code = False
                else:
                    flush()
                    in_code = True
                    block_type = "code"
                    append(line, line_number)
                continue
            if in_code:
                append(line, line_number)
                continue
            heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if heading:
                flush()
                level = len(heading.group(1))
                title = heading.group(2).strip()
                headings[:] = headings[: level - 1]
                headings.append(title)
                blocks.append(
                    ParsedBlock(
                        block_type="heading",
                        text=title,
                        heading_path=list(headings),
                        order=len(blocks),
                        source_locator={"line_start": line_number, "line_end": line_number},
                    )
                )
                continue
            if not line.strip():
                flush()
                continue
            if re.match(r"^\s*(?:[-*+] |\d+[.)] )", line):
                if buffer and block_type != "list":
                    flush()
                block_type = "list"
            elif line.lstrip().startswith(">"):
                if buffer and block_type != "quote":
                    flush()
                block_type = "quote"
            elif "|" in line and line.count("|") >= 2:
                if buffer and block_type != "table":
                    flush()
                block_type = "table"
            append(line, line_number)
        flush()
        return blocks
