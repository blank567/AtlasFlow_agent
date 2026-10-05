from __future__ import annotations

import re
from collections import defaultdict
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
        groups: dict[tuple[str, ...], list[ParsedBlock]] = defaultdict(list)
        for block in blocks:
            if block.block_type != "heading":
                groups[tuple(block.heading_path)].append(block)
        chunks: list[KnowledgeChunk] = []
        for heading_path, section in groups.items():
            for parent_blocks in self._split_blocks(section, self.parent_tokens):
                parent_content = self._render(parent_blocks, heading_path)
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
                for child_blocks in self._split_blocks(parent_blocks, self.child_tokens):
                    content = self._render(child_blocks, heading_path)
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
    def _split_blocks(blocks: list[ParsedBlock], limit: int) -> list[list[ParsedBlock]]:
        groups: list[list[ParsedBlock]] = []
        current: list[ParsedBlock] = []
        count = 0
        for block in blocks:
            size = estimate_tokens(block.text)
            if current and count + size > limit:
                groups.append(current)
                current = []
                count = 0
            current.append(block)
            count += size
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _render(blocks: list[ParsedBlock], heading_path: tuple[str, ...]) -> str:
        prefix = " > ".join(heading_path)
        body = "\n\n".join(block.text for block in blocks)
        return f"{prefix}\n\n{body}" if prefix else body

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
            metadata=dict(metadata),
        )
