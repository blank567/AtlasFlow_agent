from __future__ import annotations

from io import BytesIO

from atlasflow.knowledge.domain import ParsedBlock


class PdfDocumentParser:
    mime_types = ("application/pdf",)

    def parse(self, content: bytes) -> list[ParsedBlock]:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        blocks: list[ParsedBlock] = []
        for page_number, page in enumerate(reader.pages, 1):
            # PDF text extraction can emit NUL glyphs; PostgreSQL text/JSONB
            # rejects them even though Python strings allow them.
            text = (page.extract_text() or "").replace("\x00", "").strip()
            for paragraph in [item.strip() for item in text.split("\n\n") if item.strip()]:
                blocks.append(
                    ParsedBlock(
                        block_type="paragraph",
                        text=paragraph,
                        page_number=page_number,
                        order=len(blocks),
                        source_locator={"page_number": page_number},
                    )
                )
        return blocks
