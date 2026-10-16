from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from atlasflow.bootstrap import ProviderBundle, build_container
from atlasflow.config import Settings
from atlasflow.knowledge.api.routes import router
from atlasflow.knowledge.domain import (
    DocumentStatus,
    DocumentVersion,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeFilter,
    KnowledgeSpace,
    PrincipalContext,
    RetrievalProfile,
    RetrievalTuning,
)
from atlasflow.knowledge.domain.models import FilterValue, IndexGeneration
from atlasflow.knowledge.infrastructure import InMemoryKnowledgeRepository
from atlasflow.knowledge.ingestion import StructuralChunker
from atlasflow.knowledge.ingestion.chunker import lexicalize
from atlasflow.knowledge.ingestion.specs import capture_build_spec
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.knowledge.retrieval.scoring import pool_query_scores, ranks
from atlasflow.providers import RerankResult
from atlasflow.tools.base import ToolContext
from atlasflow.tools.knowledge_search import KnowledgeSearchArguments
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.tests.fakes import FakeEmbeddingProvider, FakeModelGateway, FakeRerankProvider


def _service(*, profile: RetrievalProfile | None = None) -> RetrievalService:
    repository = InMemoryKnowledgeRepository()
    generation = IndexGeneration(
        id="generation", space_id="space", status="active",
        embedding_profile="test", retrieval_profile="test",
    )
    space = KnowledgeSpace(
        id="space", slug="test-space", name="Test",
        active_generation_id=generation.id,
    )
    repository.spaces[space.id] = space
    repository.generations[generation.id] = generation

    async def embed(texts: list[str], *, input_type: str) -> list[list[float]]:
        assert input_type == "search_query"
        return [[1.0, 0.0] for _ in texts]

    async def rerank(
        query: str, documents: list[str], *, top_n: int,
    ) -> list[RerankResult]:
        return [RerankResult(index=index, score=0.9) for index in range(top_n)]

    service = RetrievalService(
        repository=repository,
        embedding_provider=SimpleNamespace(embed=AsyncMock(side_effect=embed)),
        rerank_provider=SimpleNamespace(rerank=AsyncMock(side_effect=rerank)),
        profile=profile,
    )
    _verify_test_generation(service)
    return service


def _verify_test_generation(service: RetrievalService) -> None:
    generation = service.repository.generations["generation"]
    embedding = service.embedding_registry.describe().model_copy(update={"observed_dimensions": 2})
    service.repository.generations[generation.id] = generation.model_copy(update={
        "identity_status": "verified", "build_spec": capture_build_spec(embedding, StructuralChunker()),
    })


def _document(
    service: RetrievalService,
    item_id: str,
    content: str,
    *,
    language: str = "en",
    space_id: str = "space",
    generation_id: str = "generation",
    status: DocumentStatus = DocumentStatus.ACTIVE,
    published: bool = True,
) -> KnowledgeChunk:
    repository = service.repository
    document = KnowledgeDocument(
        id=f"document-{item_id}", space_id=space_id, source_id=item_id,
        title=item_id, current_version_id=f"version-{item_id}" if published else None,
        status=status,
    )
    chunk = KnowledgeChunk(
        id=item_id, document_id=document.id, version_id=f"version-{item_id}",
        generation_id=generation_id, chunk_kind="child", title=item_id,
        content=content, lexical_text=lexicalize(content), token_count=10,
        embedding=[1.0, 0.0], metadata={"language": language},
    )
    repository.documents[document.id] = document
    repository.versions[chunk.version_id] = DocumentVersion(
        id=chunk.version_id, document_id=document.id, version_number=1,
        content_hash=sha256(content.encode()).hexdigest(), blob_key=f"fixture/{item_id}",
        mime_type="text/plain", byte_size=len(content.encode()), status="ready",
    )
    repository.chunks[chunk.id] = chunk
    return chunk


def _lexical_tuning(**overrides: object) -> RetrievalTuning:
    return RetrievalTuning(vector_weight=0, rerank_enabled=False, **overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "content", "language"),
    [
        ("注意力机制", "Attention mechanism compares queries with keys.", "en"),
        ("attention", "注意力机制用于建模序列关系", "zh"),
    ],
)
async def test_bilingual_expansion_recalls_other_language_without_a_model_call(
    query: str, content: str, language: str,
) -> None:
    service = _service()
    _document(service, "target", content, language=language)

    original = await service.search(
        query, space="test-space", tuning=_lexical_tuning(query_expansion_enabled=False),
    )
    expanded = await service.search(query, space="test-space", tuning=_lexical_tuning())

    assert original.hits == []
    assert [hit.chunk.id for hit in expanded.hits] == ["target"]
    assert expanded.query == query
    assert expanded.trace.query_variants[0] == query
    assert expanded.trace.query_expansion_requested is True
    assert expanded.trace.query_expansion_applied is True
    assert expanded.trace.query_expansion_method == "bilingual-terms-v1"
    assert expanded.trace.matched_terms
    assert expanded.trace.score_scope == "best_query_variant"
    assert len(expanded.trace.query_variants) <= 3
    service.embedding_provider.embed.assert_not_awaited()
    service.rerank_provider.rerank.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_query_embeds_one_batch_and_reranks_the_original_once() -> None:
    service = _service()
    _document(service, "target", "Attention mechanism uses KV cache.")
    result = await service.search("  attention 机制\n KV cache  ", space="test-space")

    original = "attention 机制 KV cache"
    assert result.query == original
    assert result.trace.query_variants[0] == original
    assert result.trace.query_expansion_applied is True
    service.embedding_provider.embed.assert_awaited_once_with(
        result.trace.query_variants, input_type="search_query",
    )
    service.rerank_provider.rerank.assert_awaited_once()
    assert service.rerank_provider.rerank.await_args.args[0] == original
    assert len({hit.chunk.id for hit in result.hits}) == len(result.hits) == 1
    assert result.trace.rerank_applied is True
    assert result.decision.score_basis == "rerank"


@pytest.mark.asyncio
async def test_unrecognized_query_preserves_single_query_embedding_and_score_scope() -> None:
    service = _service()
    _document(service, "target", "quokka migration notes")
    result = await service.search("quokka migration", space="test-space")

    assert result.trace.query_variants == ["quokka migration"]
    assert result.trace.query_expansion_requested is True
    assert result.trace.query_expansion_applied is False
    assert result.trace.matched_terms == []
    assert result.trace.score_scope == "original_query"
    service.embedding_provider.embed.assert_awaited_once_with(
        ["quokka migration"], input_type="search_query",
    )


@pytest.mark.asyncio
async def test_disabling_expansion_skips_the_expander_and_keeps_original_recall() -> None:
    service = _service()
    _document(service, "target", "Attention mechanism compares queries with keys.")
    service.query_pipeline.expand = AsyncMock(side_effect=AssertionError("must not expand"))

    result = await service.search(
        "注意力机制", space="test-space",
        tuning=_lexical_tuning(query_expansion_enabled=False),
    )

    assert result.hits == []
    assert result.trace.query_variants == ["注意力机制"]
    assert result.trace.query_expansion_requested is False
    assert result.trace.query_expansion_applied is False
    assert result.trace.query_expansion_method == "disabled"
    assert result.trace.score_scope == "original_query"
    service.query_pipeline.expand.assert_not_awaited()
    service.embedding_provider.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_expansion_tuning_is_request_scoped() -> None:
    service = _service()
    _document(service, "target", "Attention mechanism compares queries with keys.")

    disabled = await service.search(
        "注意力机制", space="test-space",
        tuning=_lexical_tuning(query_expansion_enabled=False, query_expansion_limit=1),
    )
    default = await service.search("注意力机制", space="test-space", tuning=_lexical_tuning())

    assert disabled.trace.applied_parameters["query_expansion_enabled"] is False
    assert disabled.trace.applied_parameters["query_expansion_limit"] == 1
    assert default.trace.applied_parameters["query_expansion_enabled"] is True
    assert default.trace.applied_parameters["query_expansion_limit"] == 2
    assert default.hits
    assert service.profile.query_expansion_enabled is True
    assert service.profile.query_expansion_limit == 2


def test_query_rank_pool_preserves_original_anchor_and_not_raw_score_magnitude() -> None:
    original = {"original": 1.0, "alias": 0.0, "zero": 0.0}
    expansion = {"original": 0.0, "alias": 1_000_000.0, "zero": 0.0}
    raw, pooled = pool_query_scores([original, expansion], limit=2, rrf_k=60)

    assert raw["alias"] == 1_000_000.0
    assert pooled == {"original": 1, "alias": 2}
    assert "zero" not in pooled
    assert pool_query_scores([original], limit=2, rrf_k=60) == (
        original, ranks(original, limit=2),
    )
    assert pool_query_scores([], limit=2, rrf_k=60) == ({}, {})


def test_more_aliases_cannot_amplify_a_retrieval_lane_or_introduce_invalid_matches() -> None:
    original = {"original": 1.0, "alias": 0.0}
    expansion = {
        "original": 0.0, "alias": 100.0, "negative": -1.0,
        "nan": float("nan"), "infinite": float("inf"),
    }
    _, one_alias = pool_query_scores([original, expansion], limit=2, rrf_k=60)
    _, four_aliases = pool_query_scores([original, *([expansion] * 4)], limit=2, rrf_k=60)

    assert one_alias == four_aliases == {"original": 1, "alias": 2}


@pytest.mark.asyncio
async def test_expansion_candidate_pool_stays_bounded_and_deduplicates_evidence() -> None:
    service = _service()
    for item_id in ("a", "b", "c", "d"):
        _document(service, item_id, f"Attention mechanism details {item_id}.")
    service._lexical = AsyncMock(side_effect=[
        {"a": 2.0, "b": 1.0, "c": 0.0, "d": 0.0},
        {"a": 10.0, "b": 0.0, "c": 100_000.0, "d": 99_999.0},
    ])

    result = await service.search(
        "注意力机制", space="test-space", result_limit=2,
        tuning=_lexical_tuning(candidate_limit=2, query_expansion_limit=1),
    )

    assert result.trace.lexical_candidates == 2
    assert result.trace.vector_candidates == 0
    assert result.trace.fused_candidates == 2
    assert result.trace.rerank_candidates == 0
    assert [hit.chunk.id for hit in result.hits] == ["a", "b"]
    assert result.hits[0].fusion_score == pytest.approx(1 / 61)
    assert len(result.trace.query_variants) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("malformation", ["short", "empty", "nan", "string", "bool", "dimension"])
async def test_malformed_expansion_embedding_falls_back_to_one_original_request(
    malformation: str,
) -> None:
    service = _service()
    _document(service, "target", "Attention mechanism compares queries with keys.")

    async def embed(texts: list[str], *, input_type: str) -> list[list[float]]:
        assert input_type == "search_query"
        if len(texts) == 1:
            return [[1.0, 0.0]]
        if malformation == "short":
            return [[1.0, 0.0]]
        vectors = [[1.0, 0.0] for _ in texts]
        vectors[1] = {
            "empty": [], "nan": [float("nan"), 0.0], "string": ["1", 0.0],
            "bool": [True, 0.0], "dimension": [1.0],
        }[malformation]
        return vectors

    service.embedding_provider.embed.side_effect = embed
    result = await service.search(
        "注意力机制", space="test-space", tuning=RetrievalTuning(rerank_enabled=False),
    )

    assert result.trace.degraded == ["vector_expansion_unavailable:EmbeddingCompatibilityError"]
    assert [hit.chunk.id for hit in result.hits] == ["target"]
    assert result.trace.lexical_candidates == result.trace.vector_candidates == 1
    assert result.hits[0].vector_score == pytest.approx(1.0)
    assert service.embedding_provider.embed.await_count == 2
    assert service.embedding_provider.embed.await_args_list[0].args[0] == result.trace.query_variants
    assert service.embedding_provider.embed.await_args_list[1].args[0] == ["注意力机制"]


@pytest.mark.asyncio
async def test_failed_batch_and_failed_original_embedding_keep_lexical_expansion_hits() -> None:
    service = _service()
    _document(service, "target", "Attention mechanism compares queries with keys.")
    service.embedding_provider.embed.side_effect = RuntimeError("provider unavailable")
    result = await service.search(
        "注意力机制", space="test-space", tuning=RetrievalTuning(rerank_enabled=False),
    )

    assert [hit.chunk.id for hit in result.hits] == ["target"]
    assert result.trace.vector_candidates == 0
    assert result.hits[0].vector_score == 0
    assert result.trace.degraded == [
        "vector_expansion_unavailable:RuntimeError", "vector_unavailable:RuntimeError",
    ]
    assert service.embedding_provider.embed.await_count == 2
    assert result.decision.best_relevance == pytest.approx(1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("space_kind", ["missing", "unauthorized", "empty", "filtered"])
async def test_nonsearchable_spaces_never_expand_or_call_a_provider(space_kind: str) -> None:
    service = _service()
    if space_kind != "empty":
        _document(service, "target", "Attention mechanism details.")
    service.query_pipeline.expand = AsyncMock(side_effect=AssertionError("must not expand"))
    kwargs = {"space": "missing" if space_kind == "missing" else "test-space"}
    if space_kind == "unauthorized":
        kwargs["principal"] = PrincipalContext(allowed_space_ids={"another-space"})
    elif space_kind == "filtered":
        kwargs["filters"] = KnowledgeFilter(language=FilterValue.model_validate({"in": ["zh"]}))

    if space_kind in {"missing", "unauthorized"}:
        with pytest.raises(KeyError if space_kind == "missing" else PermissionError):
            await service.search("注意力机制", **kwargs)
    else:
        result = await service.search("注意力机制", **kwargs)
        assert result.hits == []
        assert result.trace.query_variants == ["注意力机制"]
        assert result.trace.query_expansion_requested is True
        assert result.trace.query_expansion_applied is False
        assert result.trace.generation_id == "generation"
    service.query_pipeline.expand.assert_not_awaited()
    service.embedding_provider.embed.assert_not_awaited()
    service.rerank_provider.rerank.assert_not_awaited()


@pytest.mark.asyncio
async def test_expansion_uses_only_filtered_published_children_in_the_active_generation() -> None:
    service = _service()
    content = "Attention mechanism compares queries with keys."
    _document(service, "published", content)
    _document(service, "other-language", content, language="zh")
    _document(service, "other-space", content, space_id="other-space")
    _document(service, "archived", content, status=DocumentStatus.ARCHIVED)
    _document(service, "draft", content, published=False)
    _document(service, "retired", content, generation_id="retired-generation")
    stale = _document(service, "stale", content)
    service.repository.chunks[stale.id] = stale.model_copy(update={"version_id": "old-version"})
    parent = _document(service, "parent", content)
    service.repository.chunks[parent.id] = parent.model_copy(update={"chunk_kind": "parent"})
    # These deliberately nonsearchable fixture rows are not published documents.
    for item_id in ("retired", "stale", "parent"):
        document = service.repository.documents[f"document-{item_id}"]
        service.repository.documents[document.id] = document.model_copy(update={"current_version_id": None})
    filters = KnowledgeFilter(language=FilterValue.model_validate({"in": ["en"]}))

    result = await service.search(
        "注意力机制", space="test-space", filters=filters, tuning=_lexical_tuning(),
    )

    assert [hit.chunk.id for hit in result.hits] == ["published"]
    assert result.applied_filters == filters
    assert result.trace.generation_id == "generation"
    assert result.trace.lexical_candidates == result.trace.fused_candidates == 1


@pytest.mark.asyncio
async def test_query_expander_failure_preserves_original_query_retrieval() -> None:
    service = _service()
    _document(service, "target", "Attention mechanism compares queries with keys.")
    service.query_pipeline.expand = AsyncMock(side_effect=RuntimeError("expander unavailable"))

    result = await service.search("attention", space="test-space", tuning=_lexical_tuning())

    assert [hit.chunk.id for hit in result.hits] == ["target"]
    assert result.query == "attention"
    assert result.trace.query_variants == ["attention"]
    assert result.trace.query_expansion_applied is False
    assert result.trace.score_scope == "original_query"
    assert result.trace.degraded == ["query_expansion_unavailable:RuntimeError"]


@pytest.mark.asyncio
async def test_custom_expansion_cannot_replace_original_or_exceed_the_variant_budget() -> None:
    service = _service()
    _document(service, "target", "Attention mechanism compares queries with keys.")
    service.query_pipeline.expand = AsyncMock(return_value=SimpleNamespace(
        queries=["ATTENTION", " ", "x" * 4501, "  注意力  机制 ", "more", "too many"],
        matched_terms=[str(index) for index in range(20)], method="custom-expander",
    ))

    result = await service.search(
        "attention", space="test-space",
        tuning=_lexical_tuning(query_expansion_limit=1),
    )

    assert result.query == "attention"
    assert result.trace.query_variants == ["attention", "注意力 机制"]
    assert result.trace.query_expansion_method == "custom-expander"
    assert len(result.trace.matched_terms) == 8


@pytest.mark.asyncio
async def test_more_expansion_aliases_do_not_multiply_final_rrf_score() -> None:
    service = _service()
    _document(service, "target", "Attention mechanism compares queries with keys.")
    results = [
        await service.search(
            "attention", space="test-space",
            tuning=_lexical_tuning(query_expansion_limit=limit),
        )
        for limit in (1, 2, 4)
    ]

    assert all(len(result.hits) == 1 for result in results)
    assert all(result.hits[0].fusion_score == pytest.approx(1 / 61) for result in results)
    assert all(result.trace.fused_candidates == 1 for result in results)
    assert all(result.trace.lexical_candidates == 1 for result in results)


@pytest.mark.parametrize("limit", [0, 5, -1])
def test_query_expansion_limit_is_validated_by_the_search_api(limit: int) -> None:
    service = _service()
    app = FastAPI()
    app.state.container = SimpleNamespace(knowledge=service)
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/knowledge/search/debug",
            json={"query": "attention", "space": "test-space", "tuning": {"query_expansion_limit": limit}},
        )
    assert response.status_code == 422
    assert any(error["loc"][-1] == "query_expansion_limit" for error in response.json()["detail"])
    service.embedding_provider.embed.assert_not_awaited()


def test_query_expansion_api_accepts_explicit_override_and_returns_the_debug_trace() -> None:
    service = _service()
    _document(service, "target", "Attention mechanism compares queries with keys.")
    app = FastAPI()
    app.state.container = SimpleNamespace(knowledge=service)
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/knowledge/search/debug",
            json={
                "query": "注意力机制", "space": "test-space",
                "tuning": {"vector_weight": 0, "rerank_enabled": False,
                           "query_expansion_enabled": True, "query_expansion_limit": 4},
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "注意力机制"
    assert payload["hits"][0]["chunk"]["id"] == "target"
    assert payload["trace"]["query_expansion_applied"] is True
    assert payload["trace"]["applied_parameters"]["query_expansion_limit"] == 4
    assert 1 < len(payload["trace"]["query_variants"]) <= 5
    assert service.profile.query_expansion_limit == 2


def _bootstrap_settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "database_path": ":memory:", "knowledge_backend": "memory",
            "knowledge_worker_enabled": False, "knowledge_default_space": "test-space",
            "langsmith_tracing": False, "langsmith_api_key": "",
            **overrides,
        },
    )


def _fake_providers() -> ProviderBundle:
    return ProviderBundle(
        model=FakeModelGateway(), embedding=FakeEmbeddingProvider(), reranker=FakeRerankProvider(),
    )


def _clear_expansion_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "KNOWLEDGE_QUERY_EXPANSION_ENABLED", "KNOWLEDGE_QUERY_EXPANSION_LIMIT",
        "KNOWLEDGE_QUERY_ALIASES_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _seed_bootstrap_space(service: RetrievalService) -> None:
    generation = IndexGeneration(
        id="generation", space_id="space", status="active",
        embedding_profile="test", retrieval_profile="test",
    )
    service.repository.generations[generation.id] = generation
    service.repository.spaces["space"] = KnowledgeSpace(
        id="space", slug="test-space", name="Test", active_generation_id=generation.id,
    )
    _verify_test_generation(service)


@pytest.mark.asyncio
async def test_bootstrap_applies_default_expansion_settings_without_external_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_expansion_environment(monkeypatch)
    external_providers = Mock(side_effect=AssertionError("live providers must not be constructed"))
    monkeypatch.setattr("atlasflow.bootstrap.build_openrouter_providers", external_providers)
    container = build_container(_bootstrap_settings(), providers=_fake_providers())
    try:
        assert container.settings.knowledge_query_expansion_enabled is True
        assert container.settings.knowledge_query_expansion_limit == 2
        assert container.settings.knowledge_query_aliases_path == ""
        assert container.knowledge.retrieval.profile.query_expansion_enabled is True
        assert container.knowledge.retrieval.profile.query_expansion_limit == 2
        expanded = await container.knowledge.retrieval.query_pipeline.expand("注意力机制")
        assert expanded.queries[0] == "注意力机制"
        assert any("attention mechanism" in query for query in expanded.queries[1:])
        external_providers.assert_not_called()
    finally:
        await container.knowledge.close()
        await container.store.close()


@pytest.mark.asyncio
async def test_environment_can_disable_expansion_for_the_registered_agent_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_expansion_environment(monkeypatch)
    monkeypatch.setenv("KNOWLEDGE_QUERY_EXPANSION_ENABLED", "false")
    monkeypatch.setenv("KNOWLEDGE_QUERY_EXPANSION_LIMIT", "4")
    container = build_container(_bootstrap_settings(), providers=_fake_providers())
    try:
        service = container.knowledge.retrieval
        assert service.profile.query_expansion_enabled is False
        assert service.profile.query_expansion_limit == 4
        _seed_bootstrap_space(service)
        _document(service, "target", "Attention mechanism compares queries with keys.")
        service.query_pipeline.expand = AsyncMock(side_effect=AssertionError("must not expand"))
        result = await container.registry.get("knowledge_search").run(
            KnowledgeSearchArguments(query="注意力机制", space="test-space"),
            ToolContext(run_id="test-run", agent_name="researcher"),
        )
        assert result.success is True
        assert result.data["trace"]["query_expansion_requested"] is False
        assert result.data["trace"]["query_expansion_applied"] is False
        assert result.data["trace"]["query_expansion_method"] == "disabled"
        assert result.data["trace"]["query_variants"] == ["注意力机制"]
        assert result.data["trace"]["applied_parameters"]["query_expansion_limit"] == 4
        service.query_pipeline.expand.assert_not_awaited()
    finally:
        await container.knowledge.close()
        await container.store.close()


@pytest.mark.asyncio
async def test_bootstrap_loads_custom_glossary_and_same_id_overlay_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _clear_expansion_environment(monkeypatch)
    if os.name == "nt":
        assert tmp_path.resolve().drive.casefold() == "e:"
    glossary_path = tmp_path / "custom-bilingual-terms.json"
    glossary_path.write_text(json.dumps({"terms": [
        {"id": "attention", "zh": ["聚合关注机制"],
         "en": ["custom attention aggregation", "attn-custom"]},
        {"id": "quokka", "zh": ["短尾矮袋鼠"], "en": ["quokka migration"]},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_QUERY_ALIASES_PATH", str(glossary_path))
    monkeypatch.setenv("KNOWLEDGE_QUERY_EXPANSION_LIMIT", "1")
    container = build_container(_bootstrap_settings(), providers=_fake_providers())
    try:
        service = container.knowledge.retrieval
        _seed_bootstrap_space(service)
        _document(service, "custom", "Custom attention aggregation combines features.")
        result = await container.knowledge.search("聚合关注机制", tuning=_lexical_tuning())
        assert [hit.chunk.id for hit in result.hits] == ["custom"]
        assert result.trace.query_variants == ["聚合关注机制", "聚合关注机制 custom attention aggregation"]
        assert result.trace.matched_terms == ["attention"]
        assert result.trace.applied_parameters["query_expansion_limit"] == 1
        old_alias = await service.query_pipeline.expand("注意力机制")
        assert old_alias.queries == ["注意力机制"]
        assert old_alias.matched_terms == []
        preserved_builtin = await service.query_pipeline.expand("键值缓存")
        assert preserved_builtin.matched_terms == ["kv_cache"]
        new_concept = await service.query_pipeline.expand("短尾矮袋鼠")
        assert new_concept.matched_terms == ["quokka"]
        assert any("quokka migration" in query for query in new_concept.queries[1:])
        # The complete glossary is compiled once, not re-read by each query.
        glossary_path.write_text("not JSON anymore", encoding="utf-8")
        repeated = await container.knowledge.search("聚合关注机制", tuning=_lexical_tuning())
        assert repeated.trace.query_variants == result.trace.query_variants
    finally:
        await container.knowledge.close()
        await container.store.close()


@pytest.mark.parametrize(
    "payload",
    ["{broken", '{"unexpected": []}', '{"terms": [{"id": "custom", "zh": [], "en": ["thing"]}]}'],
)
def test_bad_custom_glossary_fails_bootstrap_before_any_database_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: str,
) -> None:
    _clear_expansion_environment(monkeypatch)
    if os.name == "nt":
        assert tmp_path.resolve().drive.casefold() == "e:"
    glossary_path = tmp_path / "invalid-bilingual-terms.json"
    glossary_path.write_text(payload, encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_QUERY_ALIASES_PATH", str(glossary_path))
    engine = SimpleNamespace(
        connect=Mock(side_effect=AssertionError("must not connect to PostgreSQL")),
        begin=Mock(side_effect=AssertionError("must not write PostgreSQL")),
    )
    monkeypatch.setattr(
        "atlasflow.knowledge.infrastructure.postgres_repository.create_async_engine",
        Mock(return_value=engine),
    )
    sqlite_store = Mock(side_effect=AssertionError("must not open SQLite"))
    monkeypatch.setattr("atlasflow.bootstrap.SQLiteRunStore", sqlite_store)
    settings = _bootstrap_settings(
        knowledge_backend="postgres",
        knowledge_database_url="postgresql+asyncpg://test:test@database.example.test/atlasflow_test",
        knowledge_blob_root=str(tmp_path / "blobs"),
    )
    with pytest.raises(ValueError, match="bilingual glossary"):
        build_container(settings, providers=_fake_providers())
    engine.connect.assert_not_called()
    engine.begin.assert_not_called()
    sqlite_store.assert_not_called()
