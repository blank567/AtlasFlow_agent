from __future__ import annotations

from time import perf_counter

from atlasflow.knowledge.domain import (
    KnowledgeFilter,
    PrincipalContext,
    RetrievalDecision,
    RetrievalHit,
    RetrievalProfile,
    RetrievalTrace,
    SearchResult,
)
from atlasflow.knowledge.ports.repository import KnowledgeRepository
from atlasflow.knowledge.retrieval.context_assembler import ContextAssembler
from atlasflow.knowledge.retrieval.query_pipeline import QueryPipeline
from atlasflow.knowledge.retrieval.scoring import bm25, cosine, ranks
from atlasflow.observability import traced
from atlasflow.providers import EmbeddingProvider, RerankProvider


class RetrievalService:
    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        embedding_provider: EmbeddingProvider,
        rerank_provider: RerankProvider,
        profile: RetrievalProfile | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.rerank_provider = rerank_provider
        self.profile = profile or RetrievalProfile()
        self.assembler = ContextAssembler(repository)
        self.query_pipeline = QueryPipeline()

    @traced(name="knowledge.search")
    async def search(
        self,
        query: str,
        *,
        space: str,
        filters: KnowledgeFilter | None = None,
        result_limit: int | None = None,
        principal: PrincipalContext | None = None,
    ) -> SearchResult:
        started = perf_counter()
        principal = principal or PrincipalContext()
        query = await self.query_pipeline.normalize(query)
        selected_space = await self.repository.get_space(space)
        if selected_space is None:
            raise KeyError(f"knowledge space not found: {space}")
        if principal.allowed_space_ids and selected_space.id not in principal.allowed_space_ids:
            raise PermissionError("knowledge space is not available to this principal")
        generation = await self.repository.active_generation(selected_space.id)
        chunks = await self.repository.list_searchable_chunks(selected_space.id, filters)
        limit = min(result_limit or self.profile.result_limit, 20)
        if not chunks:
            return SearchResult(
                query=query,
                decision=RetrievalDecision(
                    status="insufficient",
                    reasons=["知识空间没有可检索的已发布内容"],
                    coverage=0,
                    evidence_count=0,
                ),
                applied_filters=filters,
                trace=RetrievalTrace(
                    profile_id=self.profile.id,
                    generation_id=generation.id if generation else None,
                    duration_ms=int((perf_counter() - started) * 1000),
                ),
            )

        lexical_scores = await self._lexical(query, chunks)
        degraded: list[str] = []
        try:
            vector_scores = await self._vector(query, chunks)
        except Exception as exc:  # noqa: BLE001 - vector retrieval has a specified fallback
            vector_scores = {item.id: 0.0 for item in chunks}
            degraded.append(f"vector_unavailable:{type(exc).__name__}")
        lexical_rank = ranks(lexical_scores)
        vector_rank = ranks(vector_scores)
        fused = await self._fuse(chunks, lexical_rank, vector_rank)
        candidates = sorted(chunks, key=lambda item: (-fused[item.id], item.id))[
            : self.profile.rerank_limit
        ]
        rerank_applied = False
        rerank_scores: dict[str, float] = {}
        try:
            rerank_scores = await self._rerank(query, candidates)
            rerank_applied = bool(rerank_scores)
        except Exception as exc:  # noqa: BLE001 - reranking is an optional enhancement
            degraded.append(f"rerank_unavailable:{type(exc).__name__}")
        ordered = sorted(
            candidates,
            key=lambda item: (
                -(rerank_scores.get(item.id, fused[item.id])),
                item.id,
            ),
        )
        raw_hits = [
            RetrievalHit(
                chunk=item,
                lexical_score=lexical_scores[item.id],
                vector_score=vector_scores[item.id],
                fusion_score=fused[item.id],
                rerank_score=rerank_scores.get(item.id),
            )
            for item in ordered
        ]
        hits = await self.assembler.assemble(raw_hits, limit=limit)
        best = max(
            (
                hit.rerank_score if hit.rerank_score is not None else hit.fusion_score
                for hit in hits
            ),
            default=0,
        )
        decision = await self._decide(hits, best, limit)
        return SearchResult(
            query=query,
            decision=decision,
            hits=hits,
            applied_filters=filters,
            trace=RetrievalTrace(
                profile_id=self.profile.id,
                generation_id=generation.id if generation else None,
                lexical_candidates=len(lexical_scores),
                vector_candidates=len(vector_scores),
                fused_candidates=len(candidates),
                rerank_applied=rerank_applied,
                degraded=degraded,
                duration_ms=int((perf_counter() - started) * 1000),
            ),
        )

    @traced(name="knowledge.retrieve.lexical")
    async def _lexical(self, query: str, chunks: list) -> dict[str, float]:
        return bm25(query, chunks)

    @traced(name="knowledge.retrieve.vector")
    async def _vector(self, query: str, chunks: list) -> dict[str, float]:
        vectors = await self.embedding_provider.embed([query], input_type="search_query")
        query_vector = vectors[0]
        return {item.id: cosine(query_vector, item.embedding or []) for item in chunks}

    @traced(name="knowledge.fusion.rrf")
    async def _fuse(
        self, chunks: list, lexical_rank: dict[str, int], vector_rank: dict[str, int]
    ) -> dict[str, float]:
        return {
            item.id: self.profile.lexical_weight / (self.profile.rrf_k + lexical_rank[item.id])
            + self.profile.vector_weight / (self.profile.rrf_k + vector_rank[item.id])
            for item in chunks
        }

    @traced(name="knowledge.rerank")
    async def _rerank(self, query: str, candidates: list) -> dict[str, float]:
        rows = await self.rerank_provider.rerank(
            query,
            [item.content for item in candidates],
            top_n=min(len(candidates), self.profile.rerank_limit),
        )
        scores: dict[str, float] = {}
        used: set[int] = set()
        for row in rows:
            if 0 <= row.index < len(candidates) and row.index not in used:
                used.add(row.index)
                scores[candidates[row.index].id] = row.score
        return scores

    @traced(name="knowledge.retrieval.decision")
    async def _decide(self, hits: list[RetrievalHit], best: float, limit: int) -> RetrievalDecision:
        if not hits or best < self.profile.min_relevance:
            status = "insufficient"
            reasons = ["最高相关度低于当前未校准阈值"]
        elif len(hits) == 1:
            status = "partial"
            reasons = ["只有一个独立上下文单元，建议结合来源限制解读"]
        else:
            status = "sufficient"
            reasons = []
        return RetrievalDecision(
            status=status,
            reasons=reasons,
            coverage=min(1.0, len(hits) / max(limit, 1)),
            evidence_count=len(hits),
        )
