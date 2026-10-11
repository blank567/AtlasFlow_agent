from __future__ import annotations

import re
from io import BytesIO

from atlasflow.knowledge.domain import ParsedBlock
from atlasflow.knowledge.ingestion.parsers.common import normalize_extracted_text


class PdfDocumentParser:
    mime_types = ("application/pdf",)

    def parse(self, content: bytes) -> list[ParsedBlock]:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        blocks: list[ParsedBlock] = []
        for page_number, page in enumerate(reader.pages, 1):
            text = normalize_extracted_text(page.extract_text() or "").strip()
            for match in re.finditer(r"\S(?:.|\n)*?(?=\n\s*\n|\Z)", text):
                paragraph = match.group().strip()
                if not paragraph:
                    continue
                blocks.append(
                    ParsedBlock(
                        block_type="paragraph",
                        text=paragraph,
                        page_number=page_number,
                        order=len(blocks),
                        source_locator={
                            "page_number": page_number,
                            "source_char_start": match.start(),
                            "source_char_end": match.end(),
                        },
                    )
                )
        return blocks
