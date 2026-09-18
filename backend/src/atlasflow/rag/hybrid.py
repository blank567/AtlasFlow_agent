from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from uuid import uuid4

from atlasflow.providers import EmbeddingProvider, RerankProvider
from atlasflow.providers.openrouter import ProviderRequestError
from atlasflow.schemas import Evidence

TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9_]+")


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_PATTERN.findall(text)]


def chunk_text(text: str, *, size: int = 420, overlap: int = 80) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + size)
        chunks.append(normalized[start:end])
        if end == len(normalized):
            break
        start = max(start + 1, end - overlap)
    return chunks


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ProviderRequestError(
            f"Embedding dimensions differ: query={len(left)}, document={len(right)}"
        )
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


@dataclass(slots=True)
class Chunk:
    id: str
    document_id: str
    title: str
    content: str
    tokens: list[str]
    embedding: list[float] | None = None
    uri: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class HybridRetriever:
    """BM25 + real embeddings + RRF + real reranking."""

    def __init__(
        self, *, embedding_provider: EmbeddingProvider, rerank_provider: RerankProvider
    ) -> None:
        self.embedding_provider = embedding_provider
        self.rerank_provider = rerank_provider
        self._chunks: list[Chunk] = []
        self._embedding_lock = asyncio.Lock()

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    def ingest(
        self,
        *,
        title: str,
        content: str,
        source_id: str | None = None,
        metadata: dict[str, object] | None = None,
        uri: str | None = None,
    ) -> tuple[str, int]:
        document_id = source_id or str(uuid4())
        chunks = chunk_text(content)
        for index, part in enumerate(chunks):
            self._chunks.append(
                Chunk(
                    id=f"{document_id}:{index}",
                    document_id=document_id,
                    title=title,
                    content=part,
                    tokens=tokenize(part),
                    uri=uri,
                    metadata={**(metadata or {}), "chunk_index": index},
                )
            )
        return document_id, len(chunks)

    async def search(self, query: str, *, top_k: int = 5) -> list[Evidence]:
        if not self._chunks:
            return []

        await self._embed_pending_chunks()
        query_tokens = tokenize(query)
        query_vectors = await self.embedding_provider.embed(
            [query], input_type="search_query"
        )
        if len(query_vectors) != 1:
            raise ProviderRequestError("Embedding provider did not return one query vector")
        query_embedding = query_vectors[0]

        lexical_scores = self._bm25(query_tokens)
        vector_scores = {
            chunk.id: cosine_similarity(query_embedding, chunk.embedding or [])
            for chunk in self._chunks
        }
        lexical_rank = self._rank(lexical_scores)
        vector_rank = self._rank(vector_scores)
        fused = {
            chunk.id: 1 / (60 + lexical_rank[chunk.id]) + 1 / (60 + vector_rank[chunk.id])
            for chunk in self._chunks
        }

        candidate_count = min(len(self._chunks), max(top_k * 3, top_k))
        candidates = sorted(self._chunks, key=lambda item: fused[item.id], reverse=True)[
            :candidate_count
        ]
        reranked = await self.rerank_provider.rerank(
            query,
            [chunk.content for chunk in candidates],
            top_n=min(top_k, len(candidates)),
        )

        selected: list[tuple[Chunk, float]] = []
        used_indices: set[int] = set()
        for row in reranked:
            if row.index < 0 or row.index >= len(candidates) or row.index in used_indices:
                raise ProviderRequestError("Reranker returned an invalid or duplicate document index")
            used_indices.add(row.index)
            selected.append((candidates[row.index], row.score))

        return [
            Evidence(
                source_id=chunk.id,
                title=chunk.title,
                content=chunk.content,
                uri=chunk.uri,
                score=round(rerank_score, 6),
                metadata={
                    **chunk.metadata,
                    "document_id": chunk.document_id,
                    "lexical_score": round(lexical_scores[chunk.id], 6),
                    "vector_score": round(vector_scores[chunk.id], 6),
                    "fusion_score": round(fused[chunk.id], 6),
                    "rerank_score": round(rerank_score, 6),
                },
            )
            for chunk, rerank_score in selected
        ]

    async def _embed_pending_chunks(self) -> None:
        async with self._embedding_lock:
            pending = [chunk for chunk in self._chunks if chunk.embedding is None]
            if not pending:
                return
            vectors = await self.embedding_provider.embed(
                [chunk.content for chunk in pending], input_type="search_document"
            )
            if len(vectors) != len(pending):
                raise ProviderRequestError(
                    "Embedding provider returned a different number of document vectors"
                )
            for chunk, vector in zip(pending, vectors, strict=True):
                chunk.embedding = vector

    def _bm25(self, query_tokens: list[str]) -> dict[str, float]:
        document_count = len(self._chunks)
        average_length = sum(len(chunk.tokens) for chunk in self._chunks) / document_count
        document_frequency: Counter[str] = Counter()
        for chunk in self._chunks:
            document_frequency.update(set(chunk.tokens))

        scores: dict[str, float] = {}
        k1, b = 1.5, 0.75
        for chunk in self._chunks:
            frequencies = Counter(chunk.tokens)
            score = 0.0
            for token in query_tokens:
                frequency = frequencies[token]
                if frequency == 0:
                    continue
                df = document_frequency[token]
                inverse_document_frequency = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
                denominator = frequency + k1 * (
                    1 - b + b * len(chunk.tokens) / max(average_length, 1)
                )
                score += inverse_document_frequency * frequency * (k1 + 1) / denominator
            scores[chunk.id] = score
        return scores

    @staticmethod
    def _rank(scores: dict[str, float]) -> dict[str, int]:
        ordered = sorted(scores, key=scores.get, reverse=True)  # type: ignore[arg-type]
        return {item_id: index + 1 for index, item_id in enumerate(ordered)}

