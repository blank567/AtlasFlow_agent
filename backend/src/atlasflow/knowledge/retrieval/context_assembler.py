from __future__ import annotations

from atlasflow.knowledge.domain import KnowledgeChunk, RetrievalHit
from atlasflow.knowledge.ingestion.chunker import TOKEN_PATTERN
from atlasflow.knowledge.ports.repository import KnowledgeRepository
from atlasflow.observability import traced


class ContextAssembler:
    def __init__(self, repository: KnowledgeRepository, *, context_tokens: int = 500) -> None:
        if context_tokens < 1:
            raise ValueError("context_tokens must be positive")
        self.repository = repository
        self.context_tokens = context_tokens

    @traced(name="knowledge.context.assemble")
    async def assemble(self, hits: list[RetrievalHit], *, limit: int) -> list[RetrievalHit]:
        selected: list[RetrievalHit] = []
        seen_parents: set[str] = set()
        parent_ids = [hit.chunk.parent_id for hit in hits if hit.chunk.parent_id]
        parents = {item.id: item for item in await self.repository.get_chunks(parent_ids)}
        for hit in hits:
            identity = hit.chunk.parent_id or hit.chunk.id
            if identity in seen_parents:
                continue
            seen_parents.add(identity)
            parent = parents.get(identity)
            excerpt = self._excerpt(parent, hit.chunk) if parent else None
            selected.append(hit.model_copy(update={"context_excerpt": excerpt}))
            if len(selected) >= limit:
                break
        return selected

    def _excerpt(self, parent: KnowledgeChunk, child: KnowledgeChunk) -> str | None:
        # The heading prefix occurs only once in a parent, but on every child.
        # Match the child's body so a later child still gets its own neighborhood.
        body = child.content
        position = self._child_offset(parent, child)
        if position is not None and not parent.content.startswith(body, position):
            prefix = " > ".join(child.heading_path)
            if prefix and body.startswith(prefix + "\n\n"):
                body = body[len(prefix) + 2 :]
        if position is None or not parent.content.startswith(body, position):
            position = parent.content.find(body)
            if position < 0:
                body = child.content.split("\n\n", 1)[-1]
                position = parent.content.find(body)
            if position < 0:
                return None
        tokens = list(TOKEN_PATTERN.finditer(parent.content))
        if len(tokens) <= self.context_tokens:
            return parent.content if parent.content != child.content else None
        first = next((index for index, token in enumerate(tokens) if token.end() > position), 0)
        last = next(
            (index + 1 for index, token in enumerate(tokens) if token.end() >= position + len(body)),
            len(tokens),
        )
        width = min(self.context_tokens, len(tokens))
        start = max(0, first - max(0, width - (last - first)) // 2)
        start = min(start, len(tokens) - width)
        end = start + width
        return parent.content[tokens[start].start() : tokens[end - 1].end()]

    @staticmethod
    def _child_offset(parent: KnowledgeChunk, child: KnowledgeChunk) -> int | None:
        parent_spans = parent.metadata.get("source_spans")
        child_spans = child.metadata.get("source_spans")
        if not isinstance(parent_spans, list) or not isinstance(child_spans, list) or not child_spans:
            return None
        first = child_spans[0]
        if not isinstance(first, dict):
            return None
        prefix = " > ".join(parent.heading_path)
        offset = len(prefix) + 2 if prefix and parent.content.startswith(prefix + "\n\n") else 0
        for span in parent_spans:
            if not isinstance(span, dict):
                return None
            start, end = span.get("block_char_start"), span.get("block_char_end")
            child_start = first.get("block_char_start")
            if not isinstance(start, int) or not isinstance(end, int):
                return None
            if (
                isinstance(child_start, int)
                and span.get("order") == first.get("order")
                and span.get("page_number") == first.get("page_number")
                and start <= child_start < end
            ):
                return offset + child_start - start
            offset += end - start + 2  # the chunker joins fragments with two newlines
        return None
