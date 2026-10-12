"""Whole-space releases must never expose a partially rebuilt document manifest."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from atlasflow.knowledge.api.routes import router
from atlasflow.knowledge.application import KnowledgePlatform
from atlasflow.knowledge.domain.models import (
    CURRENT_CHUNKER_VERSION,
    CURRENT_PARSER_VERSION,
    IngestionJob,
    KnowledgeSpaceVersion,
    RetrievalTuning,
)
from atlasflow.knowledge.infrastructure import InMemoryKnowledgeRepository, MemoryBlobStore
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.providers import RerankResult
from fastapi import FastAPI

SPACE = "release-tests"


class LocalEmbeddings:
    """A deterministic provider with explicit failures for staged ingestion."""

    def __init__(self) -> None:
        self.fail_documents = False

    async def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        if self.fail_documents and input_type == "search_document":
            raise RuntimeError("test embedding outage")
        return [[1.0, float(len(text) % 7 + 1)] for text in texts]


class LocalReranker:
    async def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[RerankResult]:
        return [RerankResult(index=index, score=0.9) for index in range(len(documents))][:top_n]


@dataclass
class Harness:
    platform: KnowledgePlatform
    repository: InMemoryKnowledgeRepository
    embeddings: LocalEmbeddings


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    repository = InMemoryKnowledgeRepository()
    embeddings = LocalEmbeddings()
    platform = KnowledgePlatform(
        repository=repository,
        blob_store=MemoryBlobStore(),
        embedding_provider=embeddings,
        retrieval=RetrievalService(
            repository=repository,
            embedding_provider=embeddings,
            rerank_provider=LocalReranker(),
        ),
        default_space=SPACE,
        worker_enabled=False,
    )
    await platform.start()
    yield Harness(platform, repository, embeddings)
    await platform.close()


async def seed_space(harness: Harness, *, archive: bool = False) -> list[IngestionJob]:
    sources = [
        ("markdown", "text/markdown", b"# Retrieval\n\nEvidence about retrieval and attention."),
        ("html", "text/html", b"<h1>Attention</h1><p>Evidence about retrieval systems.</p>"),
    ]
    if archive:
        sources.append(("archived", "text/plain", b"Evidence from an archived source."))
    jobs = []
    for source_id, mime_type, content in sources:
        queued = await harness.platform.submit(
            space=SPACE, title=source_id, source_id=source_id,
            content=content, mime_type=mime_type,
        )
        completed = await harness.platform.process_job(queued.id)
        assert completed.status == "completed"
        jobs.append(completed)
    if archive:
        await harness.platform.archive_document(jobs[-1].document_id)
    return jobs


async def assert_visible_snapshot(harness: Harness, snapshot: KnowledgeSpaceVersion) -> None:
    """Check both repository visibility and the user-facing retrieval path."""
    space = await harness.platform.get_space(SPACE)
    documents = await harness.platform.list_documents(SPACE)
    expected = {
        document_id: state.version_id
        for document_id, state in snapshot.documents.items()
        if state.status == "active"
    }
    assert space.active_generation_id == snapshot.generation_id
    assert space.active_version_id == snapshot.id
    assert {
        document.id: document.current_version_id for document in documents
        if document.status == "active"
    } == expected
    searchable = await harness.repository.list_searchable_chunks(space.id)
    assert {chunk.document_id: chunk.version_id for chunk in searchable} == expected
    assert {chunk.generation_id for chunk in searchable} == {snapshot.generation_id}
    result = await harness.platform.search(
        "evidence", space=SPACE, result_limit=20,
        tuning=RetrievalTuning(vector_weight=0, rerank_enabled=False),
    )
    assert {hit.chunk.document_id: hit.chunk.version_id for hit in result.hits} == expected
    assert {hit.chunk.generation_id for hit in result.hits} == {snapshot.generation_id}


async def finish_build(harness: Harness, job_ids: list[str]) -> None:
    for job_id in job_ids:
        assert (await harness.platform.process_job(job_id)).status == "completed"


async def test_partial_and_ready_build_keep_old_search_until_atomic_publish(harness: Harness) -> None:
    original = await seed_space(harness)
    before = await harness.platform.create_space_version(SPACE, "Original whole space")
    build = await harness.platform.start_space_build(SPACE, note="Rebuild every active document")

    assert build.base_version_id == before.id
    assert build.generation_id != before.generation_id
    assert len(build.job_ids) == len(original) == 2
    assert build.status == "building"
    assert (await harness.platform.get_space(SPACE)).active_build_id == build.id
    assert {item.id for item in await harness.platform.list_space_builds(SPACE)} == {build.id}
    old_by_document = {job.document_id: job for job in original}
    staged_mimes = set()
    for job_id in build.job_ids:
        job = await harness.platform.get_job(job_id)
        version = await harness.repository.get_version(job.version_id)
        old_version = await harness.repository.get_version(old_by_document[job.document_id].version_id)
        assert version is not None and old_version is not None
        assert job.space_build_id == build.id and job.generation_id == build.generation_id
        assert job.version_id != old_version.id
        assert version.content_hash == old_version.content_hash
        assert version.parser_version == CURRENT_PARSER_VERSION
        assert version.chunker_version == CURRENT_CHUNKER_VERSION
        staged_mimes.add(version.mime_type)
    assert staged_mimes == {"text/markdown", "text/html"}

    await finish_build(harness, build.job_ids[:1])
    assert (await harness.platform.get_space_build(SPACE, build.id)).status == "building"
    await assert_visible_snapshot(harness, before)
    with pytest.raises(ValueError):
        await harness.platform.publish_space_build(SPACE, build.id)

    await finish_build(harness, build.job_ids[1:])
    assert (await harness.platform.get_space_build(SPACE, build.id)).status == "ready"
    await assert_visible_snapshot(harness, before)

    published = await harness.platform.publish_space_build(SPACE, build.id)
    assert published.documents == build.documents
    assert published.generation_id == build.generation_id
    assert published.version_number == before.version_number + 1
    assert published.note == build.note
    assert (await harness.platform.get_space(SPACE)).active_build_id is None
    finalized = await harness.platform.get_space_build(SPACE, build.id)
    assert finalized.status == "published" and finalized.published_version_id == published.id
    await assert_visible_snapshot(harness, published)
    assert (await harness.repository.get_generation(before.generation_id)).status == "retired"

    assert (await harness.platform.publish_space_build(SPACE, build.id)).id == published.id
    assert len(await harness.platform.list_space_versions(SPACE)) == 2
    chunk_count = await harness.repository.count_chunks()
    await harness.platform.activate_space_version(SPACE, before.id)
    await assert_visible_snapshot(harness, before)
    await harness.platform.activate_space_version(SPACE, published.id)
    await assert_visible_snapshot(harness, published)
    assert await harness.repository.count_chunks() == chunk_count


async def test_failed_document_keeps_entire_old_space_and_retry_can_complete(harness: Harness) -> None:
    await seed_space(harness)
    before = await harness.platform.create_space_version(SPACE, "Before failure")
    build = await harness.platform.start_space_build(SPACE)
    await finish_build(harness, build.job_ids[:1])
    harness.embeddings.fail_documents = True
    failed = await harness.platform.process_job(build.job_ids[1])
    assert failed.status == "failed_terminal"
    assert "embedding outage" in (failed.error_message or "")
    assert (await harness.platform.get_space_build(SPACE, build.id)).status == "failed"
    await assert_visible_snapshot(harness, before)
    with pytest.raises(ValueError):
        await harness.platform.publish_space_build(SPACE, build.id)

    harness.embeddings.fail_documents = False
    retried = await harness.platform.retry_job(failed.id)
    assert retried.status == "queued" and retried.error_message is None
    assert retried.version_id == failed.version_id and retried.generation_id == build.generation_id
    assert (await harness.platform.get_space_build(SPACE, build.id)).status == "building"
    await assert_visible_snapshot(harness, before)
    await finish_build(harness, [retried.id])
    assert (await harness.platform.get_space_build(SPACE, build.id)).status == "ready"
    published = await harness.platform.publish_space_build(SPACE, build.id)
    await assert_visible_snapshot(harness, published)


@pytest.mark.parametrize("operation", ["submit", "archive", "reindex", "snapshot", "activate"])
async def test_pending_build_blocks_changes_to_its_source_manifest(
    harness: Harness, operation: str,
) -> None:
    original = await seed_space(harness)
    before = await harness.platform.create_space_version(SPACE, "Stable source manifest")
    await harness.platform.start_space_build(SPACE)
    with pytest.raises(ValueError):
        if operation == "submit":
            await harness.platform.submit(
                space=SPACE, title="Additional", source_id="additional",
                content=b"Additional evidence", mime_type="text/plain",
            )
        elif operation == "archive":
            await harness.platform.archive_document(original[0].document_id)
        elif operation == "reindex":
            await harness.platform.reindex_document(original[0].document_id)
        elif operation == "snapshot":
            await harness.platform.create_space_version(SPACE, "Invalid intermediate snapshot")
        else:
            await harness.platform.activate_space_version(SPACE, before.id)
    await assert_visible_snapshot(harness, before)
    assert len(await harness.platform.list_documents(SPACE)) == 2


async def test_cancel_discards_partial_build_without_changing_visible_space(harness: Harness) -> None:
    await seed_space(harness)
    before = await harness.platform.create_space_version(SPACE, "Before cancellation")
    build = await harness.platform.start_space_build(SPACE)
    await finish_build(harness, build.job_ids[:1])
    await harness.platform.cancel_space_build(SPACE, build.id)

    assert (await harness.platform.get_space_build(SPACE, build.id)).status == "cancelled"
    assert (await harness.platform.get_space(SPACE)).active_build_id is None
    assert (await harness.platform.get_job(build.job_ids[1])).status == "cancelled"
    await assert_visible_snapshot(harness, before)
    with pytest.raises(ValueError):
        await harness.platform.retry_job(build.job_ids[1])
    with pytest.raises(ValueError):
        await harness.platform.process_job(build.job_ids[1])
    with pytest.raises(ValueError):
        await harness.platform.publish_space_build(SPACE, build.id)

    replacement = await harness.platform.start_space_build(SPACE)
    assert replacement.id != build.id and replacement.generation_id != build.generation_id
    assert len(replacement.job_ids) == 2
    await assert_visible_snapshot(harness, before)


async def test_cannot_cancel_a_running_job_or_retry_a_cancelled_failed_build(harness: Harness) -> None:
    await seed_space(harness)
    before = await harness.platform.create_space_version(SPACE, "Before running build")
    build = await harness.platform.start_space_build(SPACE)
    claimed = await harness.repository.claim_job(build.job_ids[0])
    assert claimed is not None and claimed.status == "running"
    with pytest.raises(ValueError):
        await harness.platform.cancel_space_build(SPACE, build.id)
    assert (await harness.platform.get_space(SPACE)).active_build_id == build.id

    harness.embeddings.fail_documents = True
    failed = await harness.platform.process_job(claimed.id)
    assert failed.status == "failed_terminal"
    await harness.platform.cancel_space_build(SPACE, build.id)
    with pytest.raises(ValueError):
        await harness.platform.retry_job(failed.id)
    await assert_visible_snapshot(harness, before)


async def test_archived_documents_remain_in_manifest_without_rebuilding(harness: Harness) -> None:
    original = await seed_space(harness, archive=True)
    archived = original[-1]
    before = await harness.platform.create_space_version(SPACE, "Includes archive")
    build = await harness.platform.start_space_build(SPACE)

    assert len(build.documents) == 3 and len(build.job_ids) == 2
    assert build.documents[archived.document_id] == before.documents[archived.document_id]
    assert len(await harness.repository.list_versions(archived.document_id)) == 1
    jobs = [await harness.platform.get_job(job_id) for job_id in build.job_ids]
    assert archived.document_id not in {job.document_id for job in jobs}
    await finish_build(harness, build.job_ids)
    published = await harness.platform.publish_space_build(SPACE, build.id)
    assert published.documents[archived.document_id].status == "archived"
    assert published.documents[archived.document_id].version_id == archived.version_id
    await assert_visible_snapshot(harness, published)
    await harness.platform.activate_space_version(SPACE, before.id)
    await assert_visible_snapshot(harness, before)


async def test_concurrent_build_starts_reserve_the_space_once(harness: Harness) -> None:
    await seed_space(harness)
    await harness.platform.create_space_version(SPACE, "Base")
    outcomes = await asyncio.gather(
        harness.platform.start_space_build(SPACE, note="First contender"),
        harness.platform.start_space_build(SPACE, note="Second contender"),
        return_exceptions=True,
    )
    successes = [item for item in outcomes if not isinstance(item, BaseException)]
    failures = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(successes) == 1 and len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert len(await harness.platform.list_space_builds(SPACE)) == 1
    documents = await harness.platform.list_documents(SPACE)
    for document in documents:
        assert len(await harness.repository.list_versions(document.id)) == 2


async def test_build_creates_a_base_snapshot_when_none_was_saved(harness: Harness) -> None:
    original = await seed_space(harness)
    assert (await harness.platform.get_space(SPACE)).active_version_id is None
    build = await harness.platform.start_space_build(SPACE)
    base = await harness.repository.get_space_version(build.base_version_id)
    assert base is not None
    assert {state.version_id for state in base.documents.values()} == {
        job.version_id for job in original
    }
    await assert_visible_snapshot(harness, base)


async def test_empty_space_cannot_start_a_build(harness: Harness) -> None:
    with pytest.raises(ValueError):
        await harness.platform.start_space_build(SPACE)
    assert (await harness.platform.get_space(SPACE)).active_build_id is None
    assert await harness.platform.list_space_builds(SPACE) == []


async def test_build_cannot_be_read_or_published_from_another_space(harness: Harness) -> None:
    await seed_space(harness)
    build = await harness.platform.start_space_build(SPACE)
    other = await harness.platform.create_space(slug="other-space", name="Other space")
    with pytest.raises(KeyError):
        await harness.platform.get_space_build(other.slug, build.id)
    with pytest.raises(KeyError):
        await harness.platform.publish_space_build(other.slug, build.id)
    with pytest.raises(KeyError):
        await harness.platform.cancel_space_build(other.slug, build.id)
    assert await harness.platform.list_space_builds(other.slug) == []


async def test_source_validation_fails_before_creating_partial_batch(harness: Harness) -> None:
    original = await seed_space(harness)
    before = await harness.platform.create_space_version(SPACE, "Valid indexed baseline")
    second = await harness.repository.get_version(original[1].version_id)
    assert second is not None
    await harness.platform.blob_store.put(second.blob_key, b"corrupted source")

    with pytest.raises(ValueError, match="hash"):
        await harness.platform.start_space_build(SPACE)

    assert await harness.platform.list_space_builds(SPACE) == []
    assert len(await harness.repository.list_jobs(before.space_id)) == 2
    for job in original:
        assert len(await harness.repository.list_versions(job.document_id)) == 1
    assert (await harness.platform.get_space(SPACE)).active_build_id is None
    await assert_visible_snapshot(harness, before)


async def test_publish_revalidates_index_completeness_before_any_pointer_changes(
    harness: Harness,
) -> None:
    await seed_space(harness)
    before = await harness.platform.create_space_version(SPACE, "Complete original index")
    build = await harness.platform.start_space_build(SPACE)
    await finish_build(harness, build.job_ids)
    staged_child = next(
        chunk for chunk in harness.repository.chunks.values()
        if chunk.generation_id == build.generation_id and chunk.chunk_kind == "child"
    )
    harness.repository.chunks[staged_child.id] = staged_child.model_copy(update={"embedding": None})

    with pytest.raises(ValueError):
        await harness.platform.publish_space_build(SPACE, build.id)

    assert (await harness.platform.get_space(SPACE)).active_build_id == build.id
    assert len(await harness.platform.list_space_versions(SPACE)) == 1
    await assert_visible_snapshot(harness, before)


async def test_concurrent_publish_is_idempotent_even_after_later_rollback(harness: Harness) -> None:
    await seed_space(harness)
    before = await harness.platform.create_space_version(SPACE, "Original")
    build = await harness.platform.start_space_build(SPACE)
    await finish_build(harness, build.job_ids)
    first, second = await asyncio.gather(
        harness.platform.publish_space_build(SPACE, build.id),
        harness.platform.publish_space_build(SPACE, build.id),
    )
    assert first.id == second.id
    assert len(await harness.platform.list_space_versions(SPACE)) == 2
    await assert_visible_snapshot(harness, first)

    await harness.platform.activate_space_version(SPACE, before.id)
    assert (await harness.platform.publish_space_build(SPACE, build.id)).id == first.id
    await assert_visible_snapshot(harness, before)


async def test_space_build_api_contracts_and_locked_mutations(harness: Harness) -> None:
    original = await seed_space(harness)
    app = FastAPI()
    app.state.container = SimpleNamespace(knowledge=harness.platform)
    app.include_router(router)
    path = f"/api/v1/knowledge/spaces/{SPACE}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(f"{path}/reindex", json={"note": "x" * 501})).status_code == 422
        started = await client.post(f"{path}/reindex", json={"note": "Entire space"})
        assert started.status_code == 202
        build = started.json()
        build_path = f"{path}/builds/{build['id']}"
        assert (await client.get(f"{path}/builds")).json()[0]["id"] == build["id"]
        assert (await client.get(build_path)).json()["status"] == "building"
        assert (await client.post(f"{build_path}/publish")).status_code == 409
        assert (await client.post(f"{path}/reindex", json={"note": "duplicate"})).status_code == 409
        assert (await client.post(f"{path}/documents", json={
            "title": "Locked", "content": "This cannot be imported during a space build",
        })).status_code == 409
        assert (await client.post(f"{path}/upload", files={
            "file": ("locked.txt", b"temporary text", "text/plain"),
        })).status_code == 409
        assert (await client.post(
            f"/api/v1/knowledge/documents/{original[0].document_id}/archive"
        )).status_code == 409
        other = await harness.platform.create_space(slug="api-other", name="Other")
        assert (await client.get(
            f"/api/v1/knowledge/spaces/{other.slug}/builds/{build['id']}"
        )).status_code == 404
        await finish_build(harness, build["job_ids"])
        assert (await client.get(build_path)).json()["status"] == "ready"
        released = await client.post(f"{build_path}/publish")
        assert released.status_code == 200
        assert released.json()["note"] == "Entire space"
        assert (await client.post(f"{build_path}/publish")).json()["id"] == released.json()["id"]
        assert (await client.post(f"{build_path}/cancel")).status_code == 409
        second = (await client.post(f"{path}/reindex", json={})).json()
        assert (await client.post(f"{path}/builds/{second['id']}/cancel")).json()["status"] == "cancelled"


async def test_interrupted_worker_requeues_staged_job_without_publishing(harness: Harness) -> None:
    await seed_space(harness)
    base = await harness.platform.create_space_version(SPACE, "Before shutdown")
    build = await harness.platform.start_space_build(SPACE)
    started = asyncio.Event()
    original_embed = harness.embeddings.embed

    async def paused_embed(texts, *, input_type):
        started.set()
        await asyncio.Event().wait()

    harness.embeddings.embed = paused_embed
    execution = asyncio.create_task(harness.platform.process_job(build.job_ids[0]))
    await asyncio.wait_for(started.wait(), timeout=2)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    job = await harness.platform.get_job(build.job_ids[0])
    assert job.status == "queued" and job.error_code == "WorkerInterrupted"
    await assert_visible_snapshot(harness, base)
    harness.embeddings.embed = original_embed
    await finish_build(harness, build.job_ids)
    assert (await harness.platform.get_space_build(SPACE, build.id)).status == "ready"
    await assert_visible_snapshot(harness, base)
