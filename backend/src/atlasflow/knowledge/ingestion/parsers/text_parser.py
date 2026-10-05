from __future__ import annotations

import re

from atlasflow.knowledge.domain import ParsedBlock


class TextParser:
    mime_types = ("text/plain",)

    def parse(self, content: bytes) -> list[ParsedBlock]:
        text = content.decode("utf-8-sig")
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
        return [
            ParsedBlock(block_type="paragraph", text=item, order=index)
            for index, item in enumerate(paragraphs)
        ]
