from __future__ import annotations

from atlasflow.knowledge.domain import RetrievalHit
from atlasflow.knowledge.ports.repository import KnowledgeRepository
from atlasflow.observability import traced


class ContextAssembler:
    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

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
            selected.append(hit.model_copy(update={"chunk": parent or hit.chunk}))
            if len(selected) >= limit:
                break
        return selected
