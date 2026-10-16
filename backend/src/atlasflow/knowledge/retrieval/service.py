from __future__ import annotations

import math
from time import perf_counter
from typing import Literal

from atlasflow.knowledge.domain import (
    KnowledgeFilter,
    PrincipalContext,
    RetrievalDecision,
    RetrievalHit,
    RetrievalProfile,
    RetrievalTrace,
    RetrievalTuning,
    SearchResult,
    SearchSnapshot,
)
from atlasflow.knowledge.ports.repository import KnowledgeRepository
from atlasflow.knowledge.retrieval.context_assembler import ContextAssembler
from atlasflow.knowledge.retrieval.query_pipeline import QueryPipeline
from atlasflow.knowledge.retrieval.scoring import bm25, cosine, pool_query_scores
from atlasflow.observability import traced
from atlasflow.providers import EmbeddingProvider, RerankProvider
from atlasflow.providers.embedding_registry import EmbeddingRegistry, validate_vectors


class RetrievalService:
    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        embedding_provider: EmbeddingProvider,
        rerank_provider: RerankProvider,
        profile: RetrievalProfile | None = None,
        query_pipeline: QueryPipeline | None = None,
        embedding_registry: EmbeddingRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.embedding_registry = embedding_registry or EmbeddingRegistry(embedding_provider)
        self.rerank_provider = rerank_provider
        self.profile = profile or RetrievalProfile()
        self.assembler = ContextAssembler(repository)
        self.query_pipeline = query_pipeline or QueryPipeline()

    @traced(name="knowledge.search")
    async def search(
        self,
        query: str,
        *,
        space: str,
        filters: KnowledgeFilter | None = None,
        result_limit: int | None = None,
        tuning: RetrievalTuning | None = None,
        principal: PrincipalContext | None = None,
        space_version_id: str | None = None,
        snapshot: SearchSnapshot | None = None,
    ) -> SearchResult:
        started = perf_counter()
        principal = principal or PrincipalContext()
        overrides = tuning.model_dump(exclude_none=True) if tuning else {}
        rerank_enabled = bool(overrides.pop("rerank_enabled", True))
        profile = RetrievalProfile.model_validate(
            {
                **self.profile.model_dump(),
                **overrides,
                "id": f"{self.profile.id}+experiment" if tuning and tuning.model_fields_set else self.profile.id,
            }
        )
        limit = min(result_limit or profile.result_limit, 20)
        if profile.lexical_weight == 0 and profile.vector_weight == 0:
            raise ValueError("lexical_weight and vector_weight cannot both be zero")
        if rerank_enabled and profile.rerank_limit > profile.candidate_limit:
            raise ValueError("rerank_limit cannot exceed candidate_limit")
        if limit > (profile.rerank_limit if rerank_enabled else profile.candidate_limit):
            raise ValueError("result_limit exceeds the available candidate limit")
        applied_parameters: dict[str, float | int | bool] = {
            "lexical_weight": profile.lexical_weight,
            "vector_weight": profile.vector_weight,
            "rrf_k": profile.rrf_k,
            "candidate_limit": profile.candidate_limit,
            "rerank_limit": profile.rerank_limit,
            "rerank_enabled": rerank_enabled,
            "result_limit": limit,
            "query_expansion_enabled": profile.query_expansion_enabled,
            "query_expansion_limit": profile.query_expansion_limit,
        }
        query = await self.query_pipeline.normalize(query)
        selected_space = await self.repository.get_space(space)
        if selected_space is None:
            raise KeyError(f"knowledge space not found: {space}")
        if principal.allowed_space_ids and selected_space.id not in principal.allowed_space_ids:
            raise PermissionError("knowledge space is not available to this principal")
        if snapshot is not None:
            snapshot = SearchSnapshot.model_validate(snapshot.model_dump())
            if snapshot.space_id != selected_space.id:
                raise PermissionError("pinned snapshot does not belong to the selected knowledge space")
            if space_version_id and snapshot.space_version_id != space_version_id:
                raise ValueError("space version and supplied snapshot disagree")
        else:
            snapshot = await self.repository.capture_search_snapshot(selected_space.id, space_version_id)
        chunks = await self.repository.list_snapshot_chunks(snapshot, filters)
        generation_id = snapshot.generation_id
        trace_identity = {
            "space_version_id": snapshot.space_version_id, "manifest_hash": snapshot.manifest_hash,
            "identity_status": snapshot.identity_status,
            "index_fingerprint": snapshot.build_spec.fingerprint if snapshot.build_spec else None,
            "embedding_model": snapshot.build_spec.embedding.model if snapshot.build_spec else None,
            "embedding_dimensions": snapshot.build_spec.embedding.observed_dimensions if snapshot.build_spec else None,
        }
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
                    profile_id=profile.id,
                    **trace_identity,
                    generation_id=generation_id,
                    rerank_requested=rerank_enabled,
                    query_expansion_requested=profile.query_expansion_enabled,
                    query_expansion_method=(
                        "bilingual-terms-v1" if profile.query_expansion_enabled else "disabled"
                    ),
                    query_variants=[query],
                    applied_parameters=applied_parameters,
                    duration_ms=int((perf_counter() - started) * 1000),
                ),
            )

        degraded: list[str] = []
        embedding_compatibility = "not_requested"
        query_provider = None
        if profile.vector_weight:
            try:
                if snapshot.identity_status != "verified" or snapshot.build_spec is None:
                    raise ValueError("INDEX_LEGACY_IDENTITY_UNKNOWN: semantic retrieval needs a verified index identity")
                query_provider = self.embedding_registry.resolve(snapshot.build_spec.embedding)
                embedding_compatibility = "compatible"
            except Exception as exc:
                if profile.lexical_weight == 0:
                    raise ValueError(f"VECTOR_INDEX_INCOMPATIBLE: {exc}") from exc
                embedding_compatibility = "unavailable"
                degraded.append(f"embedding_incompatible:{str(exc)[:250]}")
        queries = [query]
        matched_terms: list[str] = []
        expansion_method = "disabled" if not profile.query_expansion_enabled else "bilingual-terms-v1"
        if profile.query_expansion_enabled:
            try:
                expansion = await self.query_pipeline.expand(
                    query, enabled=True, limit=profile.query_expansion_limit
                )
                # The pipeline is replaceable, but the original query and bounded
                # cost are invariants even for a future model-backed expander.
                seen = {query.casefold()}
                for variant in expansion.queries:
                    normalized = " ".join(variant.split())
                    if (normalized and len(normalized) <= 4500
                            and normalized.casefold() not in seen):
                        seen.add(normalized.casefold())
                        queries.append(normalized)
                    if len(queries) > profile.query_expansion_limit:
                        break
                matched_terms = expansion.matched_terms[:8]
                expansion_method = expansion.method
            except Exception as exc:  # noqa: BLE001 - expansion must not break the original query
                queries = [query]
                matched_terms = []
                degraded.append(f"query_expansion_unavailable:{type(exc).__name__}")
        zero_scores = {item.id: 0.0 for item in chunks}
        lexical_rows = (
            [await self._lexical(variant, chunks) for variant in queries]
            if profile.lexical_weight > 0 else [zero_scores]
        )
        if profile.vector_weight == 0 or query_provider is None:
            vector_rows = [zero_scores]
        else:
            try:
                vector_rows = (
                    await self._vector_queries(queries, chunks, provider=query_provider, spec=snapshot.build_spec.embedding)
                    if len(queries) > 1 else [await self._vector(query, chunks, provider=query_provider, spec=snapshot.build_spec.embedding)]
                )
            except Exception as exc:
                if len(queries) > 1:
                    # Providers rejecting the batch must not suppress the original
                    # vector lane. At most one original-only fallback is attempted.
                    degraded.append(f"vector_expansion_unavailable:{type(exc).__name__}")
                    try:
                        vector_rows = [await self._vector(query, chunks, provider=query_provider, spec=snapshot.build_spec.embedding)]
                    except Exception as original_exc:
                        if profile.lexical_weight == 0:
                            raise ValueError(f"VECTOR_RETRIEVAL_UNAVAILABLE: {original_exc}") from original_exc
                        embedding_compatibility = "query_failed"
                        vector_rows = [zero_scores]
                        degraded.append(f"vector_unavailable:{type(original_exc).__name__}")
                else:
                    if profile.lexical_weight == 0:
                        raise ValueError(f"VECTOR_RETRIEVAL_UNAVAILABLE: {exc}") from exc
                    embedding_compatibility = "query_failed"
                    vector_rows = [zero_scores]
                    degraded.append(f"vector_unavailable:{type(exc).__name__}")
        lexical_scores, lexical_rank = pool_query_scores(
            lexical_rows, limit=profile.candidate_limit, rrf_k=profile.rrf_k
        )
        vector_scores, vector_rank = pool_query_scores(
            vector_rows, limit=profile.candidate_limit, rrf_k=profile.rrf_k
        )
        # RRF consumes the union of actual matches, never an arbitrary rank for
        # zero-score chunks (including when a retrieval lane is unavailable).
        eligible = [
            item for item in chunks if item.id in lexical_rank or item.id in vector_rank
        ]
        fused = await self._fuse(eligible, lexical_rank, vector_rank, profile)
        candidate_pool = sorted(eligible, key=lambda item: (-fused[item.id], item.id))[
            : profile.candidate_limit
        ]
        candidates = candidate_pool[: profile.rerank_limit] if rerank_enabled else candidate_pool
        rerank_applied = False
        rerank_scores: dict[str, float] = {}
        if rerank_enabled and candidates:
            try:
                rerank_scores = await self._rerank(query, candidates, profile)
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
        available_weight = (
            (profile.lexical_weight if lexical_rank else 0)
            + (profile.vector_weight if vector_rank else 0)
        )
        fusion_ceiling = available_weight / (profile.rrf_k + 1)
        scored_hits = [
            (
                hit.rerank_score,
                "rerank",
            )
            if hit.rerank_score is not None
            else (min(1.0, max(0.0, hit.fusion_score / fusion_ceiling)), "fusion")
            for hit in hits
        ]
        best, score_basis = max(scored_hits, key=lambda item: item[0], default=(0.0, "fusion"))
        decision = await self._decide(hits, best, limit, profile, score_basis)
        return SearchResult(
            query=query,
            decision=decision,
            hits=hits,
            applied_filters=filters,
            trace=RetrievalTrace(
                **trace_identity,
                embedding_compatibility=embedding_compatibility,
                profile_id=profile.id,
                generation_id=generation_id,
                lexical_candidates=len(lexical_rank),
                vector_candidates=len(vector_rank),
                fused_candidates=len(candidate_pool),
                rerank_candidates=len(candidates) if rerank_enabled else 0,
                rerank_requested=rerank_enabled,
                rerank_applied=rerank_applied,
                query_expansion_requested=profile.query_expansion_enabled,
                query_expansion_applied=len(queries) > 1,
                query_expansion_method=expansion_method,
                query_variants=queries,
                matched_terms=matched_terms,
                score_scope="best_query_variant" if len(queries) > 1 else "original_query",
                applied_parameters=applied_parameters,
                degraded=degraded,
                duration_ms=int((perf_counter() - started) * 1000),
            ),
        )

    @traced(name="knowledge.retrieve.lexical")
    async def _lexical(self, query: str, chunks: list) -> dict[str, float]:
        return bm25(query, chunks)

    @traced(name="knowledge.retrieve.vector")
    async def _vector(self, query: str, chunks: list, *, provider=None, spec=None) -> dict[str, float]:
        vectors = await (provider or self.embedding_provider).embed([query], input_type=spec.query_input_type if spec else "search_query")
        validate_vectors(vectors, 1, spec.observed_dimensions if spec else None)
        if spec:
            self.embedding_registry.validate_observed_model(provider or self.embedding_provider, spec)
        query_vector = vectors[0]
        return {item.id: cosine(query_vector, item.embedding or []) for item in chunks}

    @traced(name="knowledge.retrieve.vector.expanded_queries")
    async def _vector_queries(self, queries: list[str], chunks: list, *, provider=None, spec=None) -> list[dict[str, float]]:
        """Batch all variants without repeating document embedding or crossing spaces."""
        vectors = await (provider or self.embedding_provider).embed(queries, input_type=spec.query_input_type if spec else "search_query")
        validate_vectors(vectors, len(queries), spec.observed_dimensions if spec else None)
        if spec:
            self.embedding_registry.validate_observed_model(provider or self.embedding_provider, spec)
        if len(vectors) != len(queries):
            raise ValueError("query embedding count does not match query variants")
        dimensions: set[int] = set()
        for vector in vectors:
            if not isinstance(vector, list) or not vector or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) for value in vector
            ):
                raise ValueError("query embedding must be a non-empty finite numeric vector")
            dimensions.add(len(vector))
        if len(dimensions) != 1:
            raise ValueError("query embedding dimensions do not match")
        return [
            {item.id: cosine(vector, item.embedding or []) for item in chunks}
            for vector in vectors
        ]

    @traced(name="knowledge.fusion.rrf")
    async def _fuse(
        self,
        chunks: list,
        lexical_rank: dict[str, int],
        vector_rank: dict[str, int],
        profile: RetrievalProfile,
    ) -> dict[str, float]:
        return {
            item.id: (
                profile.lexical_weight / (profile.rrf_k + lexical_rank[item.id])
                if item.id in lexical_rank else 0.0
            ) + (
                profile.vector_weight / (profile.rrf_k + vector_rank[item.id])
                if item.id in vector_rank else 0.0
            )
            for item in chunks
        }

    @traced(name="knowledge.rerank")
    async def _rerank(
        self, query: str, candidates: list, profile: RetrievalProfile
    ) -> dict[str, float]:
        rows = await self.rerank_provider.rerank(
            query,
            [item.content for item in candidates],
            top_n=min(len(candidates), profile.rerank_limit),
        )
        scores: dict[str, float] = {}
        used: set[int] = set()
        for row in rows:
            if 0 <= row.index < len(candidates) and row.index not in used:
                used.add(row.index)
                scores[candidates[row.index].id] = row.score
        return scores

    @traced(name="knowledge.retrieval.decision")
    async def _decide(
        self,
        hits: list[RetrievalHit],
        best: float,
        limit: int,
        profile: RetrievalProfile,
        score_basis: Literal["rerank", "fusion"],
    ) -> RetrievalDecision:
        if not hits or best < profile.min_relevance:
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
            best_relevance=best,
            score_basis=score_basis,
        )
