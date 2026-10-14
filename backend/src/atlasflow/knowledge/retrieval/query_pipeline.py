from __future__ import annotations

import re

from atlasflow.observability import traced

from .bilingual_query_expander import BilingualQueryExpander, QueryExpander, QueryExpansion


class QueryPipeline:
    """Normalize independently, then optionally add bounded bilingual recall variants."""

    def __init__(self, expander: QueryExpander | None = None) -> None:
        self.expander = expander if expander is not None else BilingualQueryExpander()

    @traced(name="knowledge.query.normalize")
    async def normalize(self, query: str) -> str:
        normalized = re.sub(r"\s+", " ", query).strip()
        if not normalized:
            raise ValueError("knowledge query is empty after normalization")
        return normalized

    @traced(name="knowledge.query.expand")
    async def expand(
        self, query: str, *, enabled: bool = True, limit: int = 2
    ) -> QueryExpansion:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("knowledge query is empty after normalization")
        if not enabled:
            return QueryExpansion(queries=[query], matched_terms=[], method="disabled")
        return await self.expander.expand(query, limit=limit)
