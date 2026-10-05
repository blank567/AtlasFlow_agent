from __future__ import annotations

import re

from atlasflow.knowledge.domain import ParsedBlock


class MarkdownParser:
    mime_types = ("text/markdown", "text/x-markdown")

    def parse(self, content: bytes) -> list[ParsedBlock]:
        lines = content.decode("utf-8-sig").splitlines()
        blocks: list[ParsedBlock] = []
        headings: list[str] = []
        buffer: list[str] = []
        block_type = "paragraph"
        in_code = False

        def flush() -> None:
            nonlocal buffer, block_type
            text = "\n".join(buffer).strip()
            if text:
                blocks.append(
                    ParsedBlock(
                        block_type=block_type,  # type: ignore[arg-type]
                        text=text,
                        heading_path=list(headings),
                        order=len(blocks),
                    )
                )
            buffer = []
            block_type = "paragraph"

        for line in lines:
            if line.startswith("```"):
                if in_code:
                    buffer.append(line)
                    flush()
                    in_code = False
                else:
                    flush()
                    in_code = True
                    block_type = "code"
                    buffer.append(line)
                continue
            if in_code:
                buffer.append(line)
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
            buffer.append(line)
        flush()
        return blocks
