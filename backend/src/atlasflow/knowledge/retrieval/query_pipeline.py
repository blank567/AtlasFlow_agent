from __future__ import annotations

import re
from pathlib import Path

from atlasflow.knowledge.domain.index_specs import stable_hash
from atlasflow.observability import traced

from .bilingual_query_expander import BilingualQueryExpander, QueryExpander, QueryExpansion


class QueryPipeline:
    """Normalize independently, then optionally add bounded bilingual recall variants."""

    def __init__(self, expander: QueryExpander | None = None) -> None:
        self.expander = expander if expander is not None else BilingualQueryExpander()

    def describe(self) -> dict:
        """A locally computed identity plus the compiled glossary, with no model call."""
        terms = getattr(self.expander, "_terms", ())
        aliases = {"terms": [{"id": item.id, "zh": list(item.zh), "en": list(item.en)}
                             for item in terms]}
        root = Path(__file__).parent
        source = {name: (root / name).read_text(encoding="utf-8")
                  for name in ("query_pipeline.py", "bilingual_query_expander.py")}
        value = {"implementation_hash": stable_hash(source), "aliases": aliases,
                 "adapter": f"{type(self.expander).__module__}.{type(self.expander).__qualname__}"}
        return {**value, "fingerprint": stable_hash(value)}

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
