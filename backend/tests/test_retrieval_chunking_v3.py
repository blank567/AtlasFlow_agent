from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from atlasflow.knowledge.domain import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSpace,
    RetrievalTuning,
)
from atlasflow.knowledge.domain.models import IndexGeneration
from atlasflow.knowledge.infrastructure import InMemoryKnowledgeRepository
from atlasflow.knowledge.ingestion import StructuralChunker
from atlasflow.knowledge.ingestion.parsers.pdf_parser import PdfDocumentParser
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.knowledge.retrieval.scoring import ranks

from backend.tests.fakes import FakeEmbeddingProvider, FakeRerankProvider


def test_pdf_sections_survive_page_boundaries_and_tag_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        (
            "Abstract\nWe study attention.\n\n1 Introduction\nAttention is useful.\n\n"
            "2 Model\nThe model contains layers."
        ),
        "2.1 Scaled Dot-Product Attention\nQueries and keys interact.",
        "References\n1 Vaswani et al. Attention Is All You Need.\n\n[2] Another paper.",
        "[3] A third paper.\n\nAppendix\nAdditional results.",
    ]

    class Page:
        def __init__(self, content: str) -> None:
            self.content = content

        def extract_text(self) -> str:
            return self.content

    class Reader:
        def __init__(self) -> None:
            self.pages = [Page(content) for content in pages]

    monkeypatch.setattr("pypdf.PdfReader", lambda stream: Reader())
    blocks = PdfDocumentParser().parse(b"%PDF-test")
    paragraphs = [block for block in blocks if block.block_type == "paragraph"]
    assert [block.heading_path for block in paragraphs] == [
        ["Abstract"], ["1 Introduction"], ["2 Model"],
        ["2 Model", "2.1 Scaled Dot-Product Attention"],
        ["References"], ["References"], ["References"], ["Appendix"],
    ]
    assert [block.source_locator["section_role"] for block in paragraphs] == [
        "abstract", "body", "body", "body",
        "references", "references", "references", "body",
    ]
    for block in paragraphs:
        page = pages[block.page_number - 1]
        start = block.source_locator["source_char_start"]
        end = block.source_locator["source_char_end"]
        assert page[start:end] == block.text

    chunks = StructuralChunker(child_tokens=20, parent_tokens=45).chunk(
        blocks,
        document_id="document",
        version_id="version",
        generation_id="generation",
        title="Paper",
        metadata={},
    )
    reference_children = [
        chunk for chunk in chunks
        if chunk.chunk_kind == "child" and chunk.metadata.get("section_role") == "references"
    ]
    assert reference_children
    assert all(chunk.heading_path == ["References"] for chunk in reference_children)
    assert {chunk.page_number for chunk in reference_children} == {3, 4}


def test_rank_lists_omit_zero_scores_and_stop_at_each_lane_limit() -> None:
    assert ranks({"a": 3.0, "b": 0.0, "c": 2.0, "d": -0.1}, limit=2) == {
        "a": 1,
        "c": 2,
    }


def _retrieval_service() -> RetrievalService:
    repository = InMemoryKnowledgeRepository()
    generation = IndexGeneration(
        space_id="space", status="active", embedding_profile="test", retrieval_profile="test"
    )
    space = KnowledgeSpace(id="space", slug="test-space", name="Test", active_generation_id=generation.id)
    repository.spaces[space.id] = space
    repository.generations[generation.id] = generation
    for item_id in ("a", "b", "c"):
        document = KnowledgeDocument(
            id=f"document-{item_id}", space_id=space.id, source_id=item_id,
            title=item_id, current_version_id=f"version-{item_id}",
        )
        repository.documents[document.id] = document
        repository.chunks[item_id] = KnowledgeChunk(
            id=item_id, document_id=document.id, version_id=document.current_version_id,
            generation_id=generation.id, chunk_kind="child", title=item_id,
            content=f"content {item_id}", token_count=2, lexical_text=f"content {item_id}",
            embedding=[1.0, 0.0],
        )
    return RetrievalService(
        repository=repository,
        embedding_provider=FakeEmbeddingProvider(),
        rerank_provider=FakeRerankProvider(),
    )


@pytest.mark.asyncio
async def test_retrieval_fuses_only_union_of_positive_top_k_matches() -> None:
    service = _retrieval_service()
    service._lexical = AsyncMock(return_value={"a": 3.0, "b": 0.0, "c": 0.0})
    service._vector = AsyncMock(return_value={"a": 0.0, "b": 0.9, "c": 0.8})

    result = await service.search(
        "test", space="test-space", result_limit=2,
        tuning=RetrievalTuning(candidate_limit=2, rerank_enabled=False),
    )

    assert result.trace.lexical_candidates == 1
    assert result.trace.vector_candidates == 2
    assert result.trace.fused_candidates == 2
    assert {hit.chunk.id for hit in result.hits} == {"a", "b"}
    assert next(hit for hit in result.hits if hit.chunk.id == "a").fusion_score == pytest.approx(1 / 61)


@pytest.mark.asyncio
async def test_vector_failure_does_not_add_arbitrary_rrf_ranks() -> None:
    service = _retrieval_service()
    service._lexical = AsyncMock(return_value={"a": 3.0, "b": 0.0, "c": 0.0})
    service._vector = AsyncMock(side_effect=RuntimeError("embedding unavailable"))

    result = await service.search(
        "test", space="test-space", result_limit=1,
        tuning=RetrievalTuning(candidate_limit=2, rerank_enabled=False),
    )

    assert [hit.chunk.id for hit in result.hits] == ["a"]
    assert result.trace.vector_candidates == 0
    assert result.trace.fused_candidates == 1
    assert result.trace.degraded == ["vector_unavailable:RuntimeError"]


@pytest.mark.asyncio
async def test_no_positive_match_returns_no_evidence() -> None:
    service = _retrieval_service()
    service._lexical = AsyncMock(return_value={"a": 0.0, "b": 0.0, "c": 0.0})
    service._vector = AsyncMock(return_value={"a": 0.0, "b": 0.0, "c": 0.0})

    result = await service.search("test", space="test-space")

    assert result.hits == []
    assert result.decision.status == "insufficient"
    assert result.trace.fused_candidates == 0
    assert result.trace.rerank_candidates == 0


@pytest.mark.asyncio
async def test_disabled_vector_lane_makes_no_embedding_request() -> None:
    service = _retrieval_service()
    service._lexical = AsyncMock(return_value={"a": 3.0, "b": 0.0, "c": 0.0})
    service._vector = AsyncMock(side_effect=AssertionError("vector lane should be disabled"))

    result = await service.search(
        "test", space="test-space", result_limit=1,
        tuning=RetrievalTuning(vector_weight=0, candidate_limit=2, rerank_enabled=False),
    )

    assert [hit.chunk.id for hit in result.hits] == ["a"]
    assert result.trace.vector_candidates == 0
    assert result.trace.degraded == []
    service._vector.assert_not_awaited()
