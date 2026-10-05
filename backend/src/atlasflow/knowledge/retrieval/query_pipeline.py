from __future__ import annotations

import re

from atlasflow.observability import traced


class QueryPipeline:
    """Conservative normalization; model rewrite remains an optional profile step."""

    @traced(name="knowledge.query.normalize")
    async def normalize(self, query: str) -> str:
        normalized = re.sub(r"\s+", " ", query).strip()
        if not normalized:
            raise ValueError("knowledge query is empty after normalization")
        return normalized
