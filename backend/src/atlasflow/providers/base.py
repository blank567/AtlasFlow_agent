from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class RerankResult:
    index: int
    score: float


class RerankProvider(Protocol):
    async def rerank(
        self, query: str, documents: list[str], *, top_n: int
    ) -> list[RerankResult]: ...

