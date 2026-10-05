from __future__ import annotations

from atlasflow.knowledge.ingestion.parsers.html_parser import HtmlDocumentParser
from atlasflow.knowledge.ingestion.parsers.markdown_parser import MarkdownParser
from atlasflow.knowledge.ingestion.parsers.pdf_parser import PdfDocumentParser
from atlasflow.knowledge.ingestion.parsers.text_parser import TextParser
from atlasflow.knowledge.ports.parser import DocumentParser


class ParserRegistry:
    def __init__(self) -> None:
        parsers = [TextParser(), MarkdownParser(), HtmlDocumentParser(), PdfDocumentParser()]
        self._parsers = {mime_type: parser for parser in parsers for mime_type in parser.mime_types}

    def get(self, mime_type: str) -> DocumentParser:
        normalized = mime_type.split(";", 1)[0].strip().lower()
        if normalized not in self._parsers:
            raise ValueError(f"unsupported knowledge document type: {normalized}")
        return self._parsers[normalized]

    @property
    def supported_types(self) -> list[str]:
        return sorted(self._parsers)
