from __future__ import annotations

import re

from atlasflow.knowledge.domain import ParsedBlock
from atlasflow.knowledge.ingestion.parsers.common import normalize_extracted_text


class TextParser:
    mime_types = ("text/plain",)

    def parse(self, content: bytes) -> list[ParsedBlock]:
        text = normalize_extracted_text(content.decode("utf-8-sig"))
        blocks: list[ParsedBlock] = []
        for match in re.finditer(r"\S(?:.|\n)*?(?=\n\s*\n|\Z)", text):
            paragraph = match.group().strip()
            if paragraph:
                blocks.append(
                    ParsedBlock(
                        block_type="paragraph",
                        text=paragraph,
                        order=len(blocks),
                        source_locator={
                            "source_char_start": match.start(),
                            "source_char_end": match.end(),
                        },
                    )
                )
        return blocks
