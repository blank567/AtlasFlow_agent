from __future__ import annotations

import re
from uuid import uuid4

from atlasflow.knowledge.domain import KnowledgeChunk, ParsedBlock

TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9_]+|[^\s]")


def estimate_tokens(text: str) -> int:
    return len(TOKEN_PATTERN.findall(text))


def lexicalize(text: str) -> str:
    try:
        import jieba

        return " ".join(token.strip().lower() for token in jieba.cut(text) if token.strip())
    except ImportError:
        return " ".join(item.lower() for item in TOKEN_PATTERN.findall(text))


class StructuralChunker:
    def __init__(self, *, child_tokens: int = 350, parent_tokens: int = 1200) -> None:
        if child_tokens < 1 or parent_tokens < child_tokens:
            raise ValueError("chunk limits must satisfy 1 <= child_tokens <= parent_tokens")
        self.child_tokens = child_tokens
        self.parent_tokens = parent_tokens

    def chunk(
        self,
        blocks: list[ParsedBlock],
        *,
        document_id: str,
        version_id: str,
        generation_id: str,
        title: str,
        metadata: dict[str, object],
    ) -> list[KnowledgeChunk]:
        chunks: list[KnowledgeChunk] = []
        for heading_path, section in self._ordered_sections(blocks):
            prefix_tokens = estimate_tokens(" > ".join(heading_path))
            include_prefix = bool(heading_path) and prefix_tokens < self.child_tokens
            parent_body_limit = self.parent_tokens - prefix_tokens if include_prefix else self.parent_tokens
            child_body_limit = self.child_tokens - prefix_tokens if include_prefix else self.child_tokens
            for parent_blocks in self._split_blocks(section, parent_body_limit):
                parent_content = self._render(parent_blocks, heading_path, include_prefix)
                if not parent_content.strip():
                    continue
                parent_id = str(uuid4())
                parent = self._make_chunk(
                    chunk_id=parent_id,
                    parent_id=None,
                    kind="parent",
                    content=parent_content,
                    blocks=parent_blocks,
                    heading_path=heading_path,
                    document_id=document_id,
                    version_id=version_id,
                    generation_id=generation_id,
                    title=title,
                    metadata=metadata,
                )
                chunks.append(parent)
                for child_blocks in self._split_blocks(parent_blocks, child_body_limit):
                    content = self._render(child_blocks, heading_path, include_prefix)
                    chunks.append(
                        self._make_chunk(
                            chunk_id=str(uuid4()),
                            parent_id=parent_id,
                            kind="child",
                            content=content,
                            blocks=child_blocks,
                            heading_path=heading_path,
                            document_id=document_id,
                            version_id=version_id,
                            generation_id=generation_id,
                            title=title,
                            metadata=metadata,
                        )
                    )
        return chunks

    @staticmethod
    def _ordered_sections(
        blocks: list[ParsedBlock],
    ) -> list[tuple[tuple[str, ...], list[ParsedBlock]]]:
        sections: list[tuple[tuple[str, ...], list[ParsedBlock]]] = []
        current: list[ParsedBlock] = []
        current_key: tuple[tuple[str, ...], int | None] | None = None
        for block in blocks:
            if block.block_type == "heading":
                if current and current_key is not None:
                    sections.append((current_key[0], current))
                current = []
                current_key = None
                continue
            key = (tuple(block.heading_path), block.page_number)
            if current and key != current_key and current_key is not None:
                sections.append((current_key[0], current))
                current = []
            current_key = key
            current.append(block)
        if current and current_key is not None:
            sections.append((current_key[0], current))
        return sections

    @classmethod
    def _split_blocks(cls, blocks: list[ParsedBlock], limit: int) -> list[list[ParsedBlock]]:
        groups: list[list[ParsedBlock]] = []
        current: list[ParsedBlock] = []
        count = 0
        for block in blocks:
            for fragment in cls._split_block(block, limit):
                size = estimate_tokens(fragment.text)
                if current and count + size > limit:
                    groups.append(current)
                    current = []
                    count = 0
                current.append(fragment)
                count += size
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _split_block(block: ParsedBlock, limit: int) -> list[ParsedBlock]:
        matches = list(TOKEN_PATTERN.finditer(block.text))
        if not matches:
            return []
        fragments: list[ParsedBlock] = []
        base_offset = int(block.source_locator.get("block_char_start", 0))
        for first in range(0, len(matches), limit):
            last = min(first + limit, len(matches))
            start = matches[first].start()
            end = matches[last - 1].end()
            fragments.append(
                block.model_copy(
                    update={
                        "text": block.text[start:end],
                        "source_locator": {
                            **block.source_locator,
                            "block_char_start": base_offset + start,
                            "block_char_end": base_offset + end,
                        },
                    }
                )
            )
        return fragments

    @staticmethod
    def _render(
        blocks: list[ParsedBlock], heading_path: tuple[str, ...], include_prefix: bool
    ) -> str:
        prefix = " > ".join(heading_path)
        body = "\n\n".join(block.text for block in blocks)
        return f"{prefix}\n\n{body}" if include_prefix else body

    @staticmethod
    def _make_chunk(
        *,
        chunk_id: str,
        parent_id: str | None,
        kind: str,
        content: str,
        blocks: list[ParsedBlock],
        heading_path: tuple[str, ...],
        document_id: str,
        version_id: str,
        generation_id: str,
        title: str,
        metadata: dict[str, object],
    ) -> KnowledgeChunk:
        pages = [item.page_number for item in blocks if item.page_number]
        orders = [item.order for item in blocks]
        section_roles = {
            role for item in blocks
            if isinstance(role := item.source_locator.get("section_role"), str)
        }
        return KnowledgeChunk(
            id=chunk_id,
            document_id=document_id,
            version_id=version_id,
            generation_id=generation_id,
            parent_id=parent_id,
            chunk_kind=kind,  # type: ignore[arg-type]
            title=title,
            content=content,
            token_count=estimate_tokens(content),
            lexical_text=lexicalize(content),
            heading_path=list(heading_path),
            page_number=min(pages) if pages else None,
            block_start=min(orders) if orders else None,
            block_end=max(orders) if orders else None,
            metadata={
                **metadata,
                **({"section_role": section_roles.pop()} if len(section_roles) == 1 else {}),
                "source_spans": [
                    {"order": item.order, **item.source_locator}
                    for item in blocks
                ],
            },
        )
