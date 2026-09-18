import pytest
from atlasflow.rag.hybrid import HybridRetriever, chunk_text

from backend.tests.fakes import FakeEmbeddingProvider, FakeRerankProvider


def test_chunk_text_uses_overlap() -> None:
    chunks = chunk_text("a" * 900, size=400, overlap=50)
    assert len(chunks) == 3
    assert chunks[0][-50:] == chunks[1][:50]


@pytest.mark.asyncio
async def test_hybrid_search_returns_scores_and_explainability_metadata() -> None:
    retriever = HybridRetriever(
        embedding_provider=FakeEmbeddingProvider(), rerank_provider=FakeRerankProvider()
    )
    retriever.ingest(
        source_id="rag",
        title="RAG",
        content="混合检索结合关键词检索、向量检索、融合排序与重排。",
    )
    retriever.ingest(
        source_id="other",
        title="Other",
        content="这是一个无关的项目管理文档。",
    )

    results = await retriever.search("混合检索如何融合向量结果", top_k=1)

    assert results[0].title == "RAG"
    assert results[0].metadata["lexical_score"] >= 0
    assert "vector_score" in results[0].metadata
    assert "fusion_score" in results[0].metadata
    assert "rerank_score" in results[0].metadata
