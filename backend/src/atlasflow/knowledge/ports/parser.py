from __future__ import annotations

from typing import Protocol

from atlasflow.knowledge.domain import ParsedBlock


class DocumentParser(Protocol):
    mime_types: tuple[str, ...]

    def parse(self, content: bytes) -> list[ParsedBlock]: ...
