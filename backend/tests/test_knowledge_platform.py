from __future__ import annotations

import pytest
from atlasflow.knowledge.application import KnowledgePlatform
from atlasflow.knowledge.domain import IngestionJob
from atlasflow.knowledge.infrastructure import InMemoryKnowledgeRepository, MemoryBlobStore
from atlasflow.knowledge.ingestion import ParserRegistry, StructuralChunker
from atlasflow.knowledge.ingestion.parsers.pdf_parser import PdfDocumentParser
from atlasflow.knowledge.retrieval import RetrievalService

from backend.tests.fakes import FakeEmbeddingProvider, FakeRerankProvider


def make_platform() -> KnowledgePlatform:
    repository = InMemoryKnowledgeRepository()
    embedding = FakeEmbeddingProvider()
    retrieval = RetrievalService(
        repository=repository,
        embedding_provider=embedding,
        rerank_provider=FakeRerankProvider(),
    )
    return KnowledgePlatform(
        repository=repository,
        blob_store=MemoryBlobStore(),
        embedding_provider=embedding,
        retrieval=retrieval,
        default_space="user-default",
        worker_enabled=False,
    )


def test_markdown_parser_and_chunker_preserve_structure() -> None:
    blocks = (
        ParserRegistry()
        .get("text/markdown")
        .parse(
            b"# Retrieval\n\nHybrid search combines lexical and vector retrieval.\n\n"
            b"## Example\n\n```python\nprint('kept together')\n```"
        )
    )
    chunks = StructuralChunker(child_tokens=20, parent_tokens=60).chunk(
        blocks,
        document_id="document",
        version_id="version",
        generation_id="generation",
        title="RAG",
        metadata={"language": "en"},
    )

    assert any(item.heading_path == ["Retrieval"] for item in chunks)
    assert any("print('kept together')" in item.content for item in chunks)
    assert {item.chunk_kind for item in chunks} == {"parent", "child"}


def test_pdf_parser_removes_postgres_incompatible_nul(monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        def extract_text(self) -> str:
            return "Train\x00ing\n\nCompute-optimal language models"

    class Reader:
        def __init__(self) -> None:
            self.pages = [Page()]

    monkeypatch.setattr("pypdf.PdfReader", lambda stream: Reader())
    blocks = PdfDocumentParser().parse(b"%PDF-test")

    assert [block.text for block in blocks] == [
        "Training",
        "Compute-optimal language models",
    ]


@pytest.mark.asyncio
async def test_claim_specific_job_does_not_take_another_document() -> None:
    repository = InMemoryKnowledgeRepository()
    first = await repository.create_job(
        IngestionJob(space_id="space", document_id="first", version_id="v1")
    )
    second = await repository.create_job(
        IngestionJob(space_id="space", document_id="second", version_id="v2")
    )

    claimed = await repository.claim_job(second.id)

    assert claimed is not None and claimed.id == second.id
    assert (await repository.get_job(first.id)).status == "queued"  # type: ignore[union-attr]
    assert await repository.claim_job(second.id) is None


@pytest.mark.asyncio
async def test_versioned_ingestion_deduplicates_and_searches_with_source_location() -> None:
    platform = make_platform()
    await platform.start()
    job = await platform.submit(
        space="user-default",
        title="Hybrid Retrieval",
        source_id="hybrid-retrieval",
        canonical_uri="https://example.test/hybrid",
        content="# Hybrid\n\n混合检索结合关键词召回、向量召回、RRF 和重排。".encode(),
        mime_type="text/markdown",
        metadata={"language": "zh", "tags": ["rag"]},
    )
    completed = await platform.process_job(job.id)
    duplicate = await platform.submit(
        space="user-default",
        title="Hybrid Retrieval",
        source_id="hybrid-retrieval",
        canonical_uri="https://example.test/hybrid",
        content="# Hybrid\n\n混合检索结合关键词召回、向量召回、RRF 和重排。".encode(),
        mime_type="text/markdown",
    )
    result = await platform.search("混合检索怎样融合向量结果？")
    document, versions = await platform.get_document(completed.document_id)

    assert completed.status == "completed"
    assert duplicate.status == "completed"
    assert len(versions) == 1
    assert document.current_version_id == completed.version_id
    assert result.hits
    assert result.hits[0].chunk.heading_path == ["Hybrid"]
    assert result.hits[0].chunk.metadata["canonical_uri"] == "https://example.test/hybrid"
    await platform.close()


@pytest.mark.asyncio
async def test_archived_document_stops_participating_in_retrieval() -> None:
    platform = make_platform()
    await platform.start()
    job = await platform.submit(
        space="user-default",
        title="Archive Example",
        source_id="archive-example",
        content=b"This is unique archival knowledge.",
        mime_type="text/plain",
    )
    completed = await platform.process_job(job.id)
    assert (await platform.search("archival knowledge")).hits

    archived = await platform.archive_document(completed.document_id)
    result = await platform.search("archival knowledge")

    assert archived.status == "archived"
    assert result.decision.status == "insufficient"
    assert result.hits == []
    await platform.close()


@pytest.mark.asyncio
async def test_empty_platform_has_no_implicit_demo_documents() -> None:
    platform = make_platform()
    await platform.start()

    spaces = await platform.list_spaces()
    documents = await platform.list_documents("user-default")

    assert [item.slug for item in spaces] == ["user-default"]
    assert documents == []
    await platform.close()


@pytest.mark.asyncio
async def test_failed_ingestion_keeps_failure_stage_and_can_be_requeued() -> None:
    platform = make_platform()
    await platform.start()
    job = await platform.submit(
        space="user-default",
        title="Unsupported source",
        content=b"not parsed",
        mime_type="application/octet-stream",
    )

    failed = await platform.process_job(job.id)
    retried = await platform.retry_job(job.id)

    assert failed.status == "failed_terminal"
    assert failed.stage == "parsing"
    assert failed.error_code
    assert retried.status == "queued"
    assert retried.stage == "queued"
    assert retried.error_code is None
    await platform.close()
