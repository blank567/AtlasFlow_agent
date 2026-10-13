"""Historical snapshot deletion retains complete source/index and build audit records."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from atlasflow.knowledge.api.routes import router
from atlasflow.knowledge.application import KnowledgePlatform
from atlasflow.knowledge.domain import JobStatus, KnowledgeSpace, KnowledgeSpaceVersion
from atlasflow.knowledge.infrastructure import InMemoryKnowledgeRepository, MemoryBlobStore
from atlasflow.knowledge.infrastructure.postgres_repository import PostgresKnowledgeRepository
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.providers import RerankResult
from fastapi import FastAPI
from sqlalchemy.sql.dml import Update

SPACE = "snapshot-deletion"


class LocalEmbeddings:
    async def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        return [[1.0, float(len(text) % 11 + 1)] for text in texts]


class LocalReranker:
    async def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[RerankResult]:
        return [RerankResult(index=i, score=0.9) for i in range(min(top_n, len(documents)))]


@dataclass
class Harness:
    platform: KnowledgePlatform
    repository: InMemoryKnowledgeRepository
    blobs: MemoryBlobStore
    snapshots: list[KnowledgeSpaceVersion]


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    repository, blobs, embeddings = (
        InMemoryKnowledgeRepository(), MemoryBlobStore(), LocalEmbeddings()
    )
    platform = KnowledgePlatform(
        repository=repository, blob_store=blobs, embedding_provider=embeddings,
        retrieval=RetrievalService(
            repository=repository, embedding_provider=embeddings, rerank_provider=LocalReranker(),
        ),
        default_space=SPACE, worker_enabled=False,
    )
    await platform.start()
    for index in range(2):
        job = await platform.submit(
            space=SPACE, title=f"Source {index}", source_id=f"source-{index}",
            content=f"# Attention\n\nEvidence source {index} about attention.".encode(),
            mime_type="text/markdown",
        )
        assert (await platform.process_job(job.id)).status == JobStatus.COMPLETED
    snapshots = [await platform.create_space_version(SPACE, f"Snapshot {i}") for i in range(3)]
    yield Harness(platform, repository, blobs, snapshots)
    await platform.close()


def api_client(harness: Harness) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.container = SimpleNamespace(knowledge=harness.platform)
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def version_url(version: KnowledgeSpaceVersion, space: str = SPACE) -> str:
    return f"/api/v1/knowledge/spaces/{space}/versions/{version.id}"


async def test_soft_delete_hides_only_manifest_and_is_idempotent(harness: Harness) -> None:
    historical = harness.snapshots[0]
    before_space = await harness.platform.get_space(SPACE)
    before = {
        "documents": dict(harness.repository.documents),
        "versions": dict(harness.repository.versions),
        "chunks": dict(harness.repository.chunks),
        "generations": dict(harness.repository.generations),
        "jobs": dict(harness.repository.jobs),
        "blobs": dict(harness.blobs.data),
    }
    deleted = await harness.platform.delete_space_version(SPACE, historical.id)
    assert deleted.deleted_at is not None and deleted.deleted_at.utcoffset() == UTC.utcoffset(None)
    assert deleted.documents == historical.documents and deleted.generation_id == historical.generation_id
    assert (await harness.repository.get_space_version(historical.id)) == deleted
    assert historical.id not in {v.id for v in await harness.platform.list_space_versions(SPACE)}
    assert await harness.platform.delete_space_version(SPACE, historical.id) == deleted
    assert await harness.platform.get_space(SPACE) == before_space
    for name in ["documents", "versions", "chunks", "generations", "jobs"]:
        assert getattr(harness.repository, name) == before[name]
    assert harness.blobs.data == before["blobs"]


async def test_current_version_cannot_be_deleted_in_platform_or_repository(harness: Harness) -> None:
    current = harness.snapshots[-1]
    for delete in [
        harness.platform.delete_space_version(SPACE, current.id),
        harness.repository.delete_space_version(current.space_id, current.id),
    ]:
        with pytest.raises(ValueError, match="current space version"):
            await delete
    assert (await harness.repository.get_space_version(current.id)).deleted_at is None


async def test_tombstone_rejects_activation_and_note_even_with_stale_model(harness: Harness) -> None:
    historical = harness.snapshots[0]
    await harness.platform.delete_space_version(SPACE, historical.id)
    with pytest.raises(KeyError):
        await harness.platform.activate_space_version(SPACE, historical.id)
    with pytest.raises(KeyError):
        await harness.platform.set_space_version_note(SPACE, historical.id, historical.note)
    with pytest.raises(KeyError):
        await harness.repository.activate_space_version(historical)
    with pytest.raises(KeyError):
        await harness.repository.update_space_version(historical.model_copy(update={"note": "stale"}))
    assert (await harness.repository.get_space_version(historical.id)).note == historical.note


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED_RETRYABLE])
async def test_busy_ingestion_blocks_new_delete_in_both_layers(
    harness: Harness, status: JobStatus,
) -> None:
    historical = harness.snapshots[0]
    original = next(iter(harness.repository.jobs.values()))
    harness.repository.jobs["busy"] = original.model_copy(update={"id": "busy", "status": status})
    with pytest.raises(ValueError, match="active ingestion jobs"):
        await harness.platform.delete_space_version(SPACE, historical.id)
    with pytest.raises(ValueError, match="active ingestion jobs"):
        await harness.repository.delete_space_version(historical.space_id, historical.id)
    assert (await harness.repository.get_space_version(historical.id)).deleted_at is None


async def test_build_lock_blocks_new_delete_but_deleted_retry_remains_idempotent(
    harness: Harness,
) -> None:
    deleted = await harness.platform.delete_space_version(SPACE, harness.snapshots[0].id)
    build = await harness.platform.start_space_build(SPACE)
    historical = harness.snapshots[1]
    with pytest.raises(ValueError, match="staged build"):
        await harness.platform.delete_space_version(SPACE, historical.id)
    with pytest.raises(ValueError, match="active build"):
        await harness.repository.delete_space_version(historical.space_id, historical.id)
    assert await harness.platform.delete_space_version(SPACE, deleted.id) == deleted
    assert (await harness.platform.get_space(SPACE)).active_build_id == build.id


async def test_deleted_highest_number_is_not_reused(harness: Harness) -> None:
    first, _, latest = harness.snapshots
    await harness.platform.activate_space_version(SPACE, first.id)
    await harness.platform.delete_space_version(SPACE, latest.id)
    created = await harness.platform.create_space_version(SPACE, "After highest deletion")
    assert created.version_number == latest.version_number + 1
    build = await harness.platform.start_space_build(SPACE)
    for job_id in build.job_ids:
        assert (await harness.platform.process_job(job_id)).status == JobStatus.COMPLETED
    published = await harness.platform.publish_space_build(SPACE, build.id)
    assert published.version_number == created.version_number + 1


async def test_deleted_published_build_snapshot_retains_idempotence_without_reactivation(
    harness: Harness,
) -> None:
    base = harness.snapshots[-1]
    build = await harness.platform.start_space_build(SPACE)
    for job_id in build.job_ids:
        assert (await harness.platform.process_job(job_id)).status == JobStatus.COMPLETED
    published = await harness.platform.publish_space_build(SPACE, build.id)
    await harness.platform.activate_space_version(SPACE, base.id)
    deleted = await harness.platform.delete_space_version(SPACE, published.id)
    before = await harness.platform.get_space(SPACE)
    count = len(harness.repository.space_versions)
    replay = await harness.platform.publish_space_build(SPACE, build.id)
    assert replay == deleted and replay.deleted_at is not None
    assert await harness.platform.get_space(SPACE) == before
    assert len(harness.repository.space_versions) == count
    assert (await harness.repository.get_space_build(build.id)).published_version_id == deleted.id
    # The completed build's baseline also remains internally readable when deleted.
    saved = await harness.platform.create_space_version(SPACE, "New current baseline")
    tombstone_base = await harness.platform.delete_space_version(SPACE, base.id)
    assert (await harness.repository.get_space_build(build.id)).base_version_id == tombstone_base.id
    assert (await harness.platform.get_space(SPACE)).active_version_id == saved.id


async def test_cancelled_build_retains_deleted_baseline(harness: Harness) -> None:
    base = harness.snapshots[-1]
    build = await harness.platform.start_space_build(SPACE)
    await harness.platform.cancel_space_build(SPACE, build.id)
    await harness.platform.create_space_version(SPACE, "New current version")
    deleted = await harness.platform.delete_space_version(SPACE, base.id)
    assert (await harness.platform.get_space_build(SPACE, build.id)).base_version_id == deleted.id
    assert (await harness.repository.get_space_version(deleted.id)).deleted_at is not None


async def test_api_delete_list_replay_and_tombstone_guards(harness: Harness) -> None:
    historical = harness.snapshots[0]
    async with api_client(harness) as client:
        url = version_url(historical)
        deleted = await client.delete(url)
        assert deleted.status_code == 200 and deleted.json()["deleted_at"] is not None
        assert (await client.delete(url)).json() == deleted.json()
        listing = await client.get(f"/api/v1/knowledge/spaces/{SPACE}/versions")
        assert historical.id not in {item["id"] for item in listing.json()}
        assert (await client.post(f"{url}/activate")).status_code == 404
        assert (await client.put(f"{url}/note", json={"note": "changed"})).status_code == 404
        assert (await client.delete(version_url(harness.snapshots[-1]))).status_code == 409
        assert (await client.delete(f"/api/v1/knowledge/spaces/{SPACE}/versions/missing")).status_code == 404
        other = await harness.platform.create_space(slug="other-space", name="Other")
        assert (await client.delete(version_url(historical, other.slug))).status_code == 404


@pytest.mark.parametrize("busy", ["build", "queued", "running", "failed_retryable"])
async def test_api_busy_space_rejects_delete(harness: Harness, busy: str) -> None:
    historical = harness.snapshots[0]
    if busy == "build":
        await harness.platform.start_space_build(SPACE)
    else:
        original = next(iter(harness.repository.jobs.values()))
        harness.repository.jobs["busy"] = original.model_copy(
            update={"id": "busy", "status": JobStatus(busy)}
        )
    async with api_client(harness) as client:
        assert (await client.delete(version_url(historical))).status_code == 409


class StatementConnection:
    """Execute deletion decisions against local rows, recording PostgreSQL lock order."""

    def __init__(self, space: KnowledgeSpace, version: KnowledgeSpaceVersion, *, busy: bool) -> None:
        self.space, self.version, self.busy = space, version, busy
        self.events: list[tuple[str, bool]] = []
        self.updates = 0

    async def execute(self, statement):
        if isinstance(statement, Update):
            self.updates += 1
            assert statement.table.name == "knowledge_space_versions"
            self.version = KnowledgeSpaceVersion.model_validate(statement.compile().params["payload"])
            return SimpleNamespace()
        table = statement.get_final_froms()[0].name
        self.events.append((table, statement._for_update_arg is not None))
        if table == "knowledge_spaces":
            row = SimpleNamespace(payload=self.space.model_dump(mode="json"))
        elif table == "knowledge_space_versions":
            row = SimpleNamespace(payload=self.version.model_dump(mode="json"))
        else:
            assert table == "knowledge_jobs"
            statuses = next(value for value in statement.compile().params.values() if isinstance(value, list))
            assert set(statuses) == {"queued", "running", "failed_retryable"}
            row = SimpleNamespace(id="busy") if self.busy else None
        return SimpleNamespace(first=lambda: row)


class StatementEngine:
    def __init__(self, connection: StatementConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def begin(self):
        yield self.connection


@pytest.mark.parametrize("block", [None, "current", "build", "jobs", "cross_space"])
async def test_postgres_delete_rechecks_locked_rows_without_connecting_to_database(block: str | None):
    space = KnowledgeSpace(id=str(uuid4()), slug="postgres-unit", name="PostgreSQL unit")
    version = KnowledgeSpaceVersion(space_id=space.id, version_number=1)
    if block == "current":
        space = space.model_copy(update={"active_version_id": version.id})
    elif block == "build":
        space = space.model_copy(update={"active_build_id": "active-build"})
    elif block == "cross_space":
        version = version.model_copy(update={"space_id": "another-space"})
    connection = StatementConnection(space, version, busy=block == "jobs")
    repository = PostgresKnowledgeRepository.__new__(PostgresKnowledgeRepository)
    repository.engine = StatementEngine(connection)
    if block:
        with pytest.raises(KeyError if block == "cross_space" else ValueError):
            await repository.delete_space_version(space.id, version.id)
        assert connection.updates == 0
    else:
        deleted = await repository.delete_space_version(space.id, version.id)
        assert deleted.deleted_at is not None and connection.updates == 1
        # Even a later build/busy job cannot turn the no-op retry into a new mutation.
        connection.space = connection.space.model_copy(update={"active_build_id": "later-build"})
        connection.busy = True
        assert await repository.delete_space_version(space.id, version.id) == deleted
        assert connection.updates == 1
    assert connection.events[:2] == [
        ("knowledge_spaces", True), ("knowledge_space_versions", True),
    ]


def test_old_snapshot_payload_defaults_to_visible() -> None:
    payload = KnowledgeSpaceVersion(space_id="space", version_number=1).model_dump(mode="json")
    del payload["deleted_at"]
    assert KnowledgeSpaceVersion.model_validate(payload).deleted_at is None
