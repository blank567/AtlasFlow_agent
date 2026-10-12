from __future__ import annotations

from types import SimpleNamespace

import pytest
from atlasflow.knowledge.application import KnowledgePlatform
from atlasflow.knowledge.domain import IngestionJob, KnowledgeSpace, RetrievalHit, RetrievalTuning
from atlasflow.knowledge.domain.models import IndexGeneration
from atlasflow.knowledge.infrastructure import InMemoryKnowledgeRepository, MemoryBlobStore
from atlasflow.knowledge.ingestion import ParserRegistry, StructuralChunker
from atlasflow.knowledge.ingestion.parsers.pdf_parser import PdfDocumentParser
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.knowledge.retrieval.context_assembler import ContextAssembler

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


@pytest.mark.parametrize(
    ("mime_type", "content"),
    [
        ("text/plain", ("token " * 170).encode()),
        ("text/markdown", ("# Long section\n\n" + "token " * 170).encode()),
        ("text/html", ("<h1>Long section</h1><p>" + "token " * 170 + "</p>").encode()),
    ],
)
def test_all_text_formats_obey_hard_chunk_limits_and_keep_source_spans(
    mime_type: str, content: bytes
) -> None:
    blocks = ParserRegistry().get(mime_type).parse(content)
    chunks = StructuralChunker(child_tokens=20, parent_tokens=45).chunk(
        blocks,
        document_id="document",
        version_id="version",
        generation_id="generation",
        title="Long source",
        metadata={},
    )

    parents = {chunk.id: chunk for chunk in chunks if chunk.chunk_kind == "parent"}
    children = [chunk for chunk in chunks if chunk.chunk_kind == "child"]

    assert parents and children
    assert all(chunk.token_count <= 45 for chunk in parents.values())
    assert all(chunk.token_count <= 20 for chunk in children)
    assert all(chunk.parent_id in parents for chunk in children)
    assert all(chunk.metadata["source_spans"] for chunk in chunks)
    assert all("block_char_start" in chunk.metadata["source_spans"][0] for chunk in chunks)


def test_pdf_chunks_keep_page_boundaries_and_hard_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Page:
        def __init__(self, number: int) -> None:
            self.number = number

        def extract_text(self) -> str:
            return f"Page {self.number} " + "word " * 100

    class Reader:
        def __init__(self) -> None:
            self.pages = [Page(1), Page(2)]

    monkeypatch.setattr("pypdf.PdfReader", lambda stream: Reader())
    blocks = PdfDocumentParser().parse(b"%PDF-test")
    chunks = StructuralChunker(child_tokens=20, parent_tokens=45).chunk(
        blocks,
        document_id="document",
        version_id="version",
        generation_id="generation",
        title="PDF",
        metadata={},
    )

    assert {chunk.page_number for chunk in chunks} == {1, 2}
    assert all(chunk.token_count <= (20 if chunk.chunk_kind == "child" else 45) for chunk in chunks)
    assert all(
        {span["page_number"] for span in chunk.metadata["source_spans"]} == {chunk.page_number}
        for chunk in chunks
    )


def test_long_heading_stays_in_metadata_without_breaking_parent_budget() -> None:
    heading = " ".join(["heading"] * 35)
    content = f"# {heading}\n\n" + "body " * 50
    chunks = StructuralChunker(child_tokens=20, parent_tokens=45).chunk(
        ParserRegistry().get("text/markdown").parse(content.encode()),
        document_id="document",
        version_id="version",
        generation_id="generation",
        title="Long heading",
        metadata={},
    )

    assert chunks
    assert all(chunk.heading_path == [heading] for chunk in chunks)
    assert all(chunk.token_count <= (20 if chunk.chunk_kind == "child" else 45) for chunk in chunks)


@pytest.mark.asyncio
async def test_context_assembler_preserves_child_and_limits_parent_neighborhood() -> None:
    repository = InMemoryKnowledgeRepository()
    chunks = StructuralChunker(child_tokens=20, parent_tokens=80).chunk(
        ParserRegistry().get("text/plain").parse(("word " * 70).encode()),
        document_id="document",
        version_id="version",
        generation_id="generation",
        title="Source",
        metadata={},
    )
    await repository.replace_chunks("version", chunks)
    child = next(chunk for chunk in chunks if chunk.chunk_kind == "child")
    hit = RetrievalHit(chunk=child, fusion_score=0.5)

    assembled = await ContextAssembler(repository, context_tokens=30).assemble([hit], limit=1)

    assert assembled[0].chunk.id == child.id
    assert assembled[0].chunk.chunk_kind == "child"
    assert assembled[0].context_excerpt is not None
    assert assembled[0].context_excerpt.count("word") <= 30


@pytest.mark.asyncio
async def test_context_uses_child_source_span_when_identical_text_repeats() -> None:
    repository = InMemoryKnowledgeRepository()
    content = "\n\n".join(
        [" ".join(["repeat"] * 20), " ".join(["middle"] * 30),
         " ".join(["repeat"] * 20), " ".join(["tail"] * 30)]
    )
    chunks = StructuralChunker(child_tokens=20, parent_tokens=120).chunk(
        ParserRegistry().get("text/plain").parse(content.encode()),
        document_id="document",
        version_id="version",
        generation_id="generation",
        title="Repeated source",
        metadata={},
    )
    await repository.replace_chunks("version", chunks)
    repeated = [
        chunk for chunk in chunks
        if chunk.chunk_kind == "child"
        and set(chunk.content.split()) == {"repeat"}
    ]
    assert len(repeated) == 2

    hit = RetrievalHit(chunk=repeated[1])
    assembled = await ContextAssembler(repository, context_tokens=26).assemble([hit], limit=1)

    assert assembled[0].context_excerpt is not None
    assert "tail" in assembled[0].context_excerpt


@pytest.mark.asyncio
async def test_claim_specific_job_does_not_take_another_document() -> None:
    repository = InMemoryKnowledgeRepository()
    await repository.create_space(KnowledgeSpace(id="space", slug="test-space", name="Test space"))
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
async def test_reindex_creates_new_pipeline_version_and_keeps_old_until_ready() -> None:
    platform = make_platform()
    await platform.start()
    original = await platform.submit(
        space="user-default",
        title="Legacy source",
        source_id="legacy-source",
        content=b"old source text with keyword",
        mime_type="text/plain",
    )
    await platform.process_job(original.id)
    old_version = await platform.repository.get_version(original.version_id)
    assert old_version is not None
    await platform.repository.update_version(
        old_version.model_copy(
            update={"parser_version": "structured-parser-v1", "chunker_version": "parent-child-v1"}
        )
    )
    queued = await platform.reindex_document(original.document_id)
    repeated = await platform.reindex_document(original.document_id)
    document, versions = await platform.get_document(original.document_id)

    assert queued.id == repeated.id
    assert queued.status == "queued"
    assert document.current_version_id == original.version_id
    assert len(versions) == 2
    assert versions[0].blob_key == versions[1].blob_key
    assert versions[1].parser_version == "structured-parser-v3"
    assert (await platform.search("keyword")).hits[0].chunk.version_id == original.version_id

    completed = await platform.process_job(queued.id)
    document, _ = await platform.get_document(original.document_id)
    assert completed.status == "completed"
    assert document.current_version_id == queued.version_id
    assert (await platform.search("keyword")).hits[0].chunk.version_id == queued.version_id
    await platform.close()


@pytest.mark.asyncio
async def test_space_versions_restore_every_document_and_hide_later_additions() -> None:
    platform = make_platform()
    await platform.start()
    original_jobs = []
    for source in ("alpha", "beta"):
        job = await platform.submit(
            space="user-default", title=source, source_id=source,
            content=f"old {source} evidence".encode(), mime_type="text/plain",
        )
        await platform.process_job(job.id)
        original_jobs.append(job)
    first = await platform.create_space_version("user-default", "原始知识空间")
    assert first.version_number == 1
    assert len(first.documents) == 2
    updated = await platform.set_space_version_note("user-default", first.id, "统一切块前")
    assert updated.note == "统一切块前"

    next_generation = await platform.repository.create_generation(
        IndexGeneration(
            space_id=first.space_id, embedding_profile="new", retrieval_profile="new"
        )
    )
    await platform.repository.activate_generation(next_generation.id)
    assert (await platform.get_space("user-default")).active_version_id is None
    with pytest.raises(ValueError, match="complete child index"):
        await platform.create_space_version("user-default", "mixed generations")
    new_jobs = []
    for source in ("alpha", "beta", "gamma"):
        job = await platform.submit(
            space="user-default", title=source, source_id=source,
            content=f"new {source} evidence".encode(), mime_type="text/plain",
        )
        await platform.process_job(job.id)
        new_jobs.append(job)
    second = await platform.create_space_version("user-default", "更新后的知识空间")
    assert (await platform.get_space("user-default")).active_version_id == second.id
    chunk_count = await platform.repository.count_chunks()

    restored = await platform.activate_space_version("user-default", first.id)
    documents = {item.source_id: item for item in await platform.list_documents("user-default")}
    searchable = await platform.repository.list_searchable_chunks(restored.id)
    assert restored.active_generation_id == first.generation_id
    assert restored.active_version_id == first.id
    assert {documents[source].current_version_id for source in ("alpha", "beta")} == {
        job.version_id for job in original_jobs
    }
    assert documents["gamma"].status == "archived"
    assert {chunk.version_id for chunk in searchable} == {job.version_id for job in original_jobs}
    assert await platform.repository.count_chunks() == chunk_count

    current = await platform.activate_space_version("user-default", second.id)
    documents = {item.source_id: item for item in await platform.list_documents("user-default")}
    assert current.active_generation_id == next_generation.id
    assert documents["gamma"].status == "active"
    assert {chunk.version_id for chunk in await platform.repository.list_searchable_chunks(current.id)} == {
        job.version_id for job in new_jobs
    }
    await platform.close()


@pytest.mark.asyncio
async def test_space_version_blocks_busy_incomplete_and_foreign_snapshots() -> None:
    platform = make_platform()
    await platform.start()
    first = await platform.submit(
        space="user-default", title="First", source_id="first-source",
        content=b"First source", mime_type="text/plain",
    )
    with pytest.raises(ValueError, match="active ingestion jobs"):
        await platform.create_space_version("user-default", "too early")
    await platform.process_job(first.id)
    snapshot = await platform.create_space_version("user-default", "ready")
    second_space = await platform.create_space(slug="another-space", name="Another")
    with pytest.raises(KeyError, match="not found"):
        await platform.activate_space_version(second_space.slug, snapshot.id)

    changed = await platform.submit(
        space="user-default", title="First", source_id="first-source",
        content=b"Changed source", mime_type="text/plain",
    )
    with pytest.raises(ValueError, match="active ingestion jobs"):
        await platform.activate_space_version("user-default", snapshot.id)
    await platform.process_job(changed.id)

    repository = platform.repository
    assert isinstance(repository, InMemoryKnowledgeRepository)
    child = next(
        chunk for chunk in repository.chunks.values()
        if chunk.version_id == first.version_id and chunk.chunk_kind == "child"
    )
    repository.chunks[child.id] = child.model_copy(update={"embedding": None})
    with pytest.raises(ValueError, match="complete child index"):
        await platform.activate_space_version("user-default", snapshot.id)
    await platform.close()


@pytest.mark.asyncio
async def test_same_hash_upload_reindexes_legacy_version_instead_of_skipping() -> None:
    platform = make_platform()
    await platform.start()
    first = await platform.submit(
        space="user-default",
        title="Legacy source",
        source_id="legacy-source",
        content=b"legacy original",
        mime_type="text/plain",
    )
    await platform.process_job(first.id)
    version = await platform.repository.get_version(first.version_id)
    assert version is not None
    await platform.repository.update_version(
        version.model_copy(update={"parser_version": "structured-parser-v1"})
    )

    second = await platform.submit(
        space="user-default",
        title="Legacy source",
        source_id="legacy-source",
        content=b"legacy original",
        mime_type="text/plain",
    )

    assert second.status == "queued"
    assert second.version_id != first.version_id
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
async def test_retrieval_lab_tuning_is_request_scoped() -> None:
    platform = make_platform()
    await platform.start()
    for index in range(3):
        job = await platform.submit(
            space="user-default",
            title=f"RAG note {index}",
            source_id=f"rag-note-{index}",
            content=f"Hybrid retrieval combines lexical and vector evidence {index}.".encode(),
            mime_type="text/plain",
        )
        await platform.process_job(job.id)

    tuned = await platform.search(
        "hybrid retrieval evidence",
        result_limit=1,
        tuning=RetrievalTuning(
            candidate_limit=2,
            rerank_limit=1,
            lexical_weight=2,
            vector_weight=0.5,
            rrf_k=20,
        ),
    )
    default = await platform.search("hybrid retrieval evidence")

    assert tuned.trace.fused_candidates == 2
    assert tuned.trace.rerank_candidates == 1
    assert tuned.trace.rerank_applied is True
    assert tuned.trace.applied_parameters["candidate_limit"] == 2
    assert tuned.trace.applied_parameters["lexical_weight"] == 2
    assert tuned.trace.profile_id.endswith("+experiment")
    assert default.trace.applied_parameters["candidate_limit"] == 40
    assert default.trace.profile_id == "default-hybrid-v1"
    await platform.close()


@pytest.mark.asyncio
async def test_retrieval_lab_can_skip_rerank_and_reject_invalid_bounds() -> None:
    platform = make_platform()
    await platform.start()
    job = await platform.submit(
        space="user-default",
        title="RAG note",
        source_id="rag-note",
        content=b"Hybrid retrieval combines lexical and vector evidence.",
        mime_type="text/plain",
    )
    await platform.process_job(job.id)

    result = await platform.search(
        "hybrid retrieval",
        result_limit=1,
        tuning=RetrievalTuning(candidate_limit=1, rerank_enabled=False),
    )
    assert result.trace.rerank_requested is False
    assert result.trace.rerank_candidates == 0
    assert result.trace.rerank_applied is False
    assert result.trace.degraded == []
    assert result.trace.fused_candidates == 1
    assert result.decision.score_basis == "fusion"
    assert result.decision.best_relevance > 0

    with pytest.raises(ValueError, match="cannot both be zero"):
        await platform.search(
            "hybrid retrieval",
            tuning=RetrievalTuning(lexical_weight=0, vector_weight=0),
        )
    await platform.close()


@pytest.mark.asyncio
async def test_retrieval_weights_change_rrf_order() -> None:
    service = make_platform().retrieval
    chunks = [SimpleNamespace(id="lexical"), SimpleNamespace(id="vector")]
    lexical_rank = {"lexical": 1, "vector": 2}
    vector_rank = {"lexical": 2, "vector": 1}
    lexical_first = await service._fuse(
        chunks,
        lexical_rank,
        vector_rank,
        service.profile.model_copy(update={"lexical_weight": 2, "vector_weight": 0}),
    )
    vector_first = await service._fuse(
        chunks,
        lexical_rank,
        vector_rank,
        service.profile.model_copy(update={"lexical_weight": 0, "vector_weight": 2}),
    )

    assert lexical_first["lexical"] > lexical_first["vector"]
    assert vector_first["vector"] > vector_first["lexical"]


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
