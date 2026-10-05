from __future__ import annotations

from html.parser import HTMLParser
from typing import ClassVar

from atlasflow.knowledge.domain import ParsedBlock


class _VisibleHTMLParser(HTMLParser):
    BLOCKS: ClassVar[set[str]] = {
        "p",
        "li",
        "pre",
        "code",
        "blockquote",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_tag: str | None = None
        self.current: list[str] = []
        self.rows: list[tuple[str, str]] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        if not self.skip_depth and tag in self.BLOCKS:
            self._flush()
            self.current_tag = tag

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        if not self.skip_depth and tag == self.current_tag:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and self.current_tag:
            self.current.append(data)

    def _flush(self) -> None:
        text = " ".join(" ".join(self.current).split())
        if self.current_tag and text:
            self.rows.append((self.current_tag, text))
        self.current = []
        self.current_tag = None


class HtmlDocumentParser:
    mime_types = ("text/html", "application/xhtml+xml")

    def parse(self, content: bytes) -> list[ParsedBlock]:
        parser = _VisibleHTMLParser()
        parser.feed(content.decode("utf-8-sig", errors="replace"))
        parser._flush()
        headings: list[str] = []
        blocks: list[ParsedBlock] = []
        for tag, text in parser.rows:
            if tag.startswith("h") and tag[1:].isdigit():
                level = int(tag[1:])
                headings[:] = headings[: level - 1]
                headings.append(text)
                kind = "heading"
            elif tag in {"pre", "code"}:
                kind = "code"
            elif tag == "blockquote":
                kind = "quote"
            elif tag in {"td", "th"}:
                kind = "table"
            elif tag == "li":
                kind = "list"
            else:
                kind = "paragraph"
            blocks.append(
                ParsedBlock(
                    block_type=kind,  # type: ignore[arg-type]
                    text=text,
                    heading_path=list(headings),
                    order=len(blocks),
                )
            )
        return blocks
