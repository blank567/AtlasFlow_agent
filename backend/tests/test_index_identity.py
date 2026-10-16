"""Index identity and historical reads use deterministic providers, never live APIs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from atlasflow.knowledge.api.routes import router
from atlasflow.knowledge.application import KnowledgePlatform
from atlasflow.knowledge.application.legacy_repair import repair_legacy_jobs
from atlasflow.knowledge.domain import RetrievalTuning, SearchSnapshot
from atlasflow.knowledge.domain.index_specs import EmbeddingSpec, stable_hash
from atlasflow.knowledge.infrastructure import InMemoryKnowledgeRepository, MemoryBlobStore
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.providers.openrouter import OpenRouterEmbeddingProvider
from fastapi import FastAPI

from backend.tests.fakes import FakeRerankProvider


class Vectors:
    def __init__(self, model="vectors-a", dimension=2):
        self.model = model
        self.dimension = dimension
        self.calls = []

    async def embed(self, texts, *, input_type):
        self.calls.append((list(texts), input_type))
        return [[1.0] * self.dimension for _ in texts]


def platform(provider=None, repository=None, blob=None):
    provider = provider or Vectors()
    repository = repository or InMemoryKnowledgeRepository()
    return KnowledgePlatform(
        repository=repository, blob_store=blob or MemoryBlobStore(),
        embedding_provider=provider, retrieval=RetrievalService(repository=repository,
            embedding_provider=provider, rerank_provider=FakeRerankProvider()),
        default_space="identity-tests", worker_enabled=False,
    )


async def ingest(app, source="original", content="attention original evidence"):
    job = await app.submit(space="identity-tests", title=source, source_id=source,
        content=content.encode(), mime_type="text/plain")
    result = await app.process_job(job.id)
    assert result.status == "completed", result.error_message
    return result


def test_identity_hash_is_stable_and_does_not_include_keys():
    spec = EmbeddingSpec(provider="openrouter", service_id="configured-service", model="model-a")
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})
    assert spec.request_hash == spec.model_copy(update={"observed_dimensions": 2}).request_hash
    assert spec.fingerprint != spec.model_copy(update={"observed_dimensions": 2}).fingerprint
    assert spec.request_hash != spec.model_copy(update={"model": "model-b"}).request_hash
    with pytest.raises(ValueError):
        EmbeddingSpec(provider="x", service_id="https://secret@host", model="model-a")
    with pytest.raises(ValueError):
        EmbeddingSpec(provider="x", service_id="service", model="https://arbitrary-host/model")


@pytest.mark.asyncio
async def test_first_import_is_bound_before_execution_and_seals_dimension_once():
    app = platform()
    await app.start()
    job = await app.submit(space="identity-tests", title="Original", content=b"attention evidence", mime_type="text/plain")
    generation = await app.repository.get_generation(job.generation_id)
    assert job.build_request_hash == generation.build_spec.request_hash
    assert generation.identity_status == "declared"
    completed = await app.process_job(job.id)
    assert completed.status == "completed"
    generation = await app.repository.get_generation(job.generation_id)
    assert generation.identity_status == "verified"
    assert generation.build_spec.embedding.observed_dimensions == 2
    with pytest.raises(ValueError, match="DIMENSION_MISMATCH"):
        await app.repository.confirm_generation_spec(generation.id, job.build_request_hash, 3)
    hit = (await app.search("attention")).hits[0]
    version = await app.repository.get_version(job.version_id)
    assert hit.chunk.metadata["source_id"]
    assert hit.chunk.metadata["content_hash"] == version.content_hash
    await app.close()


@pytest.mark.asyncio
async def test_same_dimension_different_model_cannot_write_or_reuse_same_content():
    vectors = Vectors()
    app = platform(vectors)
    await app.start()
    original = await ingest(app)
    vectors.model = "vectors-b"
    blocked = await app.submit(space="identity-tests", title="Original", source_id="original",
        content=b"attention original evidence", mime_type="text/plain")
    assert blocked.status == "failed_terminal"
    assert blocked.error_code == "IndexSpecMismatch"
    assert blocked.version_id != original.version_id
    assert (await app.repository.get_document(original.document_id)).current_version_id == original.version_id
    assert await app.blob_store.get((await app.repository.get_version(blocked.version_id)).blob_key)
    assert len(vectors.calls) == 1
    mixed = await app.search("attention")
    assert mixed.hits
    assert mixed.trace.embedding_compatibility == "unavailable"
    with pytest.raises(ValueError, match="VECTOR_INDEX_INCOMPATIBLE"):
        await app.search("attention", tuning=RetrievalTuning(lexical_weight=0))
    await app.close()


@pytest.mark.asyncio
async def test_queued_job_uses_bound_identity_not_restarted_worker_settings():
    old = platform()
    await old.start()
    job = await old.submit(space="identity-tests", title="Queued", content=b"queued source", mime_type="text/plain")
    new = platform(Vectors("vectors-b"), old.repository, old.blob_store)
    result = await new.process_job(job.id)
    assert result.status == "failed_terminal"
    assert "worker configuration" in result.error_message
    assert new.embedding_provider.calls == []
    assert await old.repository.count_chunks() == 0
    cancelled = await new.cancel_job(job.id)
    assert cancelled.status == "cancelled"
    assert await new.cancel_job(job.id) == cancelled
    assert await new.blob_store.get((await new.repository.get_version(job.version_id)).blob_key)
    await old.close()


@pytest.mark.asyncio
async def test_legacy_snapshot_migrates_without_backfilling_and_pending_source_is_preserved():
    app = platform()
    await app.start()
    original = await ingest(app)
    generation = await app.repository.get_generation(original.generation_id)
    legacy = generation.model_copy(update={"identity_status": "legacy_unknown", "build_spec": None})
    app.repository.generations[legacy.id] = legacy
    before = dict(app.repository.chunks)
    baseline = await app.create_space_version("identity-tests", "legacy")
    result = await app.search("attention")
    assert result.trace.identity_status == "legacy_unknown"
    assert result.trace.vector_candidates == 0
    blocked = await app.submit(space="identity-tests", title="Unpublished", source_id="pending",
        content=b"retained pending original", mime_type="text/plain")
    assert blocked.status == "failed_terminal"
    await app.cancel_job(blocked.id)
    with pytest.raises(ValueError, match="published source"):
        await app.start_space_build("identity-tests")
    await app.archive_document(blocked.document_id)
    build = await app.start_space_build("identity-tests", "upgrade")
    for job_id in build.job_ids:
        assert (await app.process_job(job_id)).status == "completed"
    current = await app.space_builds.publish("identity-tests", build.id)
    assert current.generation_id != baseline.generation_id
    assert await app.repository.get_generation(legacy.id) == legacy.model_copy(update={"status": "retired"})
    assert all(app.repository.chunks[key] == value for key, value in before.items())
    assert await app.blob_store.get((await app.repository.get_version(blocked.version_id)).blob_key)
    old = await app.search("attention", space_version_id=baseline.id)
    assert old.trace.identity_status == "legacy_unknown"
    assert (await app.get_space("identity-tests")).active_version_id == current.id
    await app.close()


@pytest.mark.asyncio
async def test_self_contained_snapshot_survives_publish_activation_and_soft_delete():
    app = platform()
    await app.start()
    await ingest(app)
    first = await app.create_space_version("identity-tests", "first")
    pinned = await app.capture_search_snapshot(space_version_id=first.id)
    frozen = SearchSnapshot.model_validate(pinned.model_dump(mode="json"))
    build = await app.start_space_build("identity-tests")
    for job_id in build.job_ids:
        await app.process_job(job_id)
    second = await app.space_builds.publish("identity-tests", build.id)
    await app.delete_space_version("identity-tests", first.id)
    old = await app.search("attention", snapshot=frozen)
    assert old.trace.space_version_id == first.id
    assert old.trace.generation_id == first.generation_id
    assert old.trace.manifest_hash == frozen.manifest_hash
    assert (await app.get_space("identity-tests")).active_version_id == second.id
    with pytest.raises(KeyError):
        await app.capture_search_snapshot(space_version_id=first.id)
    payload = frozen.model_dump()
    payload["generation_id"] = second.generation_id
    with pytest.raises(ValueError, match="manifest hash"):
        SearchSnapshot.model_validate(payload)
    await app.close()


@pytest.mark.asyncio
async def test_historical_model_uses_current_trusted_client_without_remote_target_from_database():
    class Client:
        base_url = "https://configured.example/api/v1"

        def __init__(self):
            self.calls = []

        async def embed(self, *, model, texts, input_type):
            self.calls.append(model)
            return [[1.0, 1.0] for _ in texts]

    client = Client()
    provider = OpenRouterEmbeddingProvider(client, "model-a")
    app = platform(provider)
    await app.start()
    await ingest(app)
    snapshot = await app.capture_search_snapshot()
    provider.model = "model-b"
    result = await app.search("attention", snapshot=snapshot)
    assert result.trace.embedding_compatibility == "compatible"
    assert client.calls[-1] == "model-a"
    untrusted = snapshot.build_spec.embedding.model_copy(update={"service_id": "another-service"})
    with pytest.raises(ValueError, match="SERVICE_UNAVAILABLE"):
        app.embedding_registry.resolve(untrusted)
    assert "configured.example" not in snapshot.model_dump_json()
    await app.close()


@pytest.mark.asyncio
async def test_cancel_api_refuses_running_build_completed_jobs_and_preserves_blob():
    app = platform()
    await app.start()
    completed = await ingest(app)
    queued = await app.submit(space="identity-tests", title="Queued", content=b"pending", mime_type="text/plain")
    web = FastAPI()
    web.state.container = SimpleNamespace(knowledge=app)
    web.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://test") as client:
        assert (await client.post(f"/api/v1/knowledge/jobs/{completed.id}/cancel")).status_code == 409
        assert (await client.post("/api/v1/knowledge/jobs/missing/cancel")).status_code == 404
        assert (await client.post(f"/api/v1/knowledge/jobs/{queued.id}/cancel")).status_code == 200
        assert (await client.post(f"/api/v1/knowledge/jobs/{queued.id}/cancel")).status_code == 200
    with pytest.raises(ValueError, match="cancelled"):
        await app.process_job(queued.id)
    await app.close()


@pytest.mark.asyncio
async def test_concurrent_first_imports_share_one_generation_and_dimension_seal():
    app = platform()
    await app.start()
    jobs = await asyncio.gather(*[
        app.submit(space="identity-tests", title=f"Source {index}", content=b"attention", mime_type="text/plain")
        for index in range(4)
    ])
    assert len({job.generation_id for job in jobs}) == 1
    results = await asyncio.gather(*(app.process_job(job.id) for job in jobs))
    assert all(result.status == "completed" for result in results)
    assert len(app.repository.generations) == 1
    await app.close()


@pytest.mark.asyncio
async def test_unbound_legacy_job_does_not_call_embedding_and_is_cancellable():
    app = platform()
    await app.start()
    queued = await app.submit(space="identity-tests", title="Queued", content=b"pending original", mime_type="text/plain")
    legacy = queued.model_copy(update={"generation_id": None, "build_request_hash": None})
    app.repository.jobs[queued.id] = legacy
    result = await app.process_job(queued.id)
    assert result.status == "failed_terminal"
    assert "INDEX_LEGACY_JOB_UNBOUND" in result.error_message
    assert app.embedding_provider.calls == []
    assert (await app.cancel_job(queued.id)).status == "cancelled"
    await app.close()


@pytest.mark.asyncio
async def test_legacy_running_repair_is_explicit_dry_run_and_never_touches_sources():
    app = platform()
    await app.start()
    queued = await app.submit(space="identity-tests", title="Queued", content=b"retained original", mime_type="text/plain")
    from atlasflow.knowledge.domain import JobStatus

    legacy = queued.model_copy(update={"generation_id": None, "build_request_hash": None, "status": JobStatus.RUNNING})
    # model_copy intentionally bypasses validation; normalize the imported old JSON.
    from atlasflow.knowledge.domain import IngestionJob

    legacy = IngestionJob.model_validate(legacy.model_dump())
    app.repository.jobs[legacy.id] = legacy
    version = await app.repository.get_version(legacy.version_id)
    preview = await repair_legacy_jobs(app.repository, "identity-tests")
    assert preview["dry_run"] is True and len(preview["jobs"]) == 1
    assert (await app.get_job(legacy.id)).status == "running"
    with pytest.raises(ValueError, match="queued or failed"):
        await app.cancel_job(legacy.id)
    repaired = await repair_legacy_jobs(app.repository, "identity-tests", confirm_no_workers=True)
    assert repaired["dry_run"] is False
    assert repaired["jobs"][0]["error_code"] == "LegacyJobUnbound"
    assert (await app.cancel_job(legacy.id)).status == "cancelled"
    assert await app.repository.get_version(version.id) == version
    assert await app.blob_store.get(version.blob_key) == b"retained original"
    assert app.embedding_provider.calls == []
    assert (await repair_legacy_jobs(app.repository, "identity-tests", confirm_no_workers=True))["jobs"] == []
    await app.close()


@pytest.mark.asyncio
async def test_changed_chunk_limits_require_whole_space_rebuild_and_update_version():
    app = platform()
    await app.start()
    original = await ingest(app)
    app.chunker.child_tokens = 200
    blocked = await app.submit(space="identity-tests", title="Original", source_id="original",
        content=b"attention original evidence", mime_type="text/plain")
    assert blocked.status == "failed_terminal"
    assert blocked.build_request_hash != original.build_request_hash
    with pytest.raises(ValueError, match="cancel this job"):
        await app.retry_job(blocked.id)
    build = await app.start_space_build("identity-tests")
    for job_id in build.job_ids:
        assert (await app.process_job(job_id)).status == "completed"
    await app.publish_space_build("identity-tests", build.id)
    generation = await app.repository.active_generation(original.space_id)
    assert generation.build_spec.child_tokens == 200
    assert (await app.repository.get_version(original.version_id)).build_request_hash == original.build_request_hash
    await app.close()


@pytest.mark.asyncio
async def test_query_dimension_mismatch_is_explicit_for_vector_only_and_lexical_can_degrade():
    provider = Vectors()
    app = platform(provider)
    await app.start()
    await ingest(app)
    provider.dimension = 3
    mixed = await app.search("attention", tuning=RetrievalTuning(query_expansion_enabled=False))
    assert mixed.hits and any("vector_unavailable" in reason for reason in mixed.trace.degraded)
    with pytest.raises(ValueError, match="VECTOR_RETRIEVAL_UNAVAILABLE"):
        await app.search("attention", tuning=RetrievalTuning(lexical_weight=0, query_expansion_enabled=False))
    await app.close()


@pytest.mark.parametrize("confirmation", [False, True])
def test_repair_cli_defaults_to_preview_and_requires_explicit_worker_confirmation(monkeypatch, confirmation):
    from atlasflow.knowledge import cli

    calls = []

    async def fake_repair(space, confirm):
        calls.append((space, confirm))

    monkeypatch.setattr(cli, "run_repair", fake_repair)
    arguments = ["atlasflow-knowledge", "repair-legacy-jobs", "--space", "identity-tests"]
    if confirmation:
        arguments.append("--confirm-no-workers")
    monkeypatch.setattr("sys.argv", arguments)
    cli.main()
    assert calls == [("identity-tests", confirmation)]


@pytest.mark.asyncio
async def test_failed_final_commit_does_not_replace_the_published_document(monkeypatch):
    app = platform()
    await app.start()
    original = await ingest(app)
    original_commit = app.repository.complete_ingestion_job

    async def crash(job_id):
        raise RuntimeError("injected failure before atomic commit")

    monkeypatch.setattr(app.repository, "complete_ingestion_job", crash)
    queued = await app.submit(space="identity-tests", title="Updated", source_id="original",
        content=b"new attention evidence", mime_type="text/plain")
    failed = await app.process_job(queued.id)
    assert failed.status == "failed_terminal"
    assert (await app.repository.get_document(original.document_id)).current_version_id == original.version_id
    assert (await app.repository.get_version(original.version_id)).status == "ready"
    monkeypatch.setattr(app.repository, "complete_ingestion_job", original_commit)
    await app.retry_job(queued.id)
    assert (await app.process_job(queued.id)).status == "completed"
    assert (await app.repository.get_document(original.document_id)).current_version_id == queued.version_id
    await app.close()
