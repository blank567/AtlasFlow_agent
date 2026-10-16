"""Optional real PostgreSQL checks, isolated in a disposable schema.

Set ATLASFLOW_TEST_DATABASE_URL explicitly to run. Production tables are never
used: every repository table is translated to a newly generated test schema.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from atlasflow.knowledge.application import KnowledgePlatform
from atlasflow.knowledge.application.legacy_repair import repair_legacy_jobs
from atlasflow.knowledge.domain import IndexGeneration, KnowledgeDocument, RetrievalTuning
from atlasflow.knowledge.infrastructure import MemoryBlobStore
from atlasflow.knowledge.infrastructure.postgres_repository import (
    PostgresKnowledgeRepository,
    chunks,
    jobs,
    metadata,
)
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.providers import RerankResult
from sqlalchemy import select, update
from sqlalchemy.schema import CreateSchema, DropSchema


class TestEmbeddings:
    fail = False

    async def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("local embedding test failure")
        return [[1.0, float(len(text) % 11 + 1)] for text in texts]


class TestReranker:
    async def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[RerankResult]:
        return [RerankResult(index=i, score=0.9) for i in range(min(top_n, len(documents)))]


@pytest.mark.skipif(not os.getenv("ATLASFLOW_TEST_DATABASE_URL"), reason="isolated PostgreSQL opt-in")
async def test_postgres_staging_failure_atomic_release_rollback_and_cancel() -> None:
    repository = PostgresKnowledgeRepository(os.environ["ATLASFLOW_TEST_DATABASE_URL"])
    original_engine = repository.engine
    schema = f"atlasflow_build_test_{uuid4().hex}"
    async with original_engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
    repository.engine = original_engine.execution_options(schema_translate_map={None: schema})
    embeddings = TestEmbeddings()
    platform = KnowledgePlatform(
        repository=repository, blob_store=MemoryBlobStore(), embedding_provider=embeddings,
        retrieval=RetrievalService(repository=repository, embedding_provider=embeddings,
                                   rerank_provider=TestReranker()),
        default_space="pg-space", worker_enabled=False,
    )
    try:
        async with repository.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        await platform.start()
        for mime, content in [
            ("text/markdown", b"# Evidence\n\nEvidence from markdown"),
            ("text/html", b"<h1>Evidence</h1><p>Evidence from HTML</p>"),
        ]:
            job = await platform.submit(space="pg-space", title=mime, mime_type=mime, content=content)
            assert (await platform.process_job(job.id)).status == "completed"
        base = await platform.create_space_version("pg-space", "Original PostgreSQL space")
        pinned = await platform.capture_search_snapshot("pg-space", base.id)
        assert pinned.identity_status == "verified"
        assert pinned.build_spec.embedding.observed_dimensions == 2
        assert pinned.documents

        async def check(snapshot):
            result = await platform.search("evidence", space="pg-space", result_limit=5,
                tuning=RetrievalTuning(vector_weight=0, rerank_enabled=False))
            assert {hit.chunk.version_id for hit in result.hits} == {
                state.version_id for state in snapshot.documents.values()
            }
            assert result.trace.generation_id == snapshot.generation_id
            assert (await platform.get_space("pg-space")).active_version_id == snapshot.id

        build = await platform.start_space_build("pg-space", "Complete staged release")
        assert (await platform.process_job(build.job_ids[0])).status == "completed"
        await check(base)
        with pytest.raises(ValueError):
            await repository.create_document(KnowledgeDocument(
                space_id=base.space_id, source_id="locked", title="Cannot bypass build lock",
            ))
        claimed = await repository.claim_job(build.job_ids[1])
        assert claimed is not None
        with pytest.raises(ValueError):
            await platform.cancel_space_build("pg-space", build.id)
        embeddings.fail = True
        assert (await platform.process_job(claimed.id)).status == "failed_terminal"
        assert (await platform.get_space_build("pg-space", build.id)).status == "failed"
        with pytest.raises(ValueError):
            await platform.publish_space_build("pg-space", build.id)
        await check(base)
        embeddings.fail = False
        await platform.retry_job(claimed.id)
        assert (await platform.process_job(claimed.id)).status == "completed"
        await check(base)
        first, second = await asyncio.gather(
            platform.publish_space_build("pg-space", build.id),
            platform.publish_space_build("pg-space", build.id),
        )
        assert first.id == second.id
        assert len(await platform.list_space_versions("pg-space")) == 2
        await check(first)
        historical = await platform.search("evidence", space="pg-space", snapshot=pinned,
            tuning=RetrievalTuning(vector_weight=0, rerank_enabled=False))
        assert historical.trace.generation_id == base.generation_id
        assert historical.trace.manifest_hash == pinned.manifest_hash
        assert (await platform.get_space("pg-space")).active_version_id == first.id
        await platform.activate_space_version("pg-space", base.id)
        await check(base)
        assert (await platform.publish_space_build("pg-space", build.id)).id == first.id
        await check(base)
        later = await platform.start_space_build("pg-space", "Cancelled release")
        assert (await platform.cancel_space_build("pg-space", later.id)).status == "cancelled"
        assert await repository.claim_job(later.job_ids[0]) is None
        with pytest.raises(ValueError):
            await platform.process_job(later.job_ids[0])
        await check(base)

        async def index_rows():
            """Compare stored vectors, not only currently searchable chunk counts."""
            async with repository.engine.connect() as connection:
                rows = (await connection.execute(select(
                    chunks.c.id, chunks.c.payload, chunks.c.embedding,
                ))).all()
            return {
                row.id: (
                    row.payload,
                    tuple(float(value) for value in row.embedding)
                    if row.embedding is not None else None,
                )
                for row in rows
            }

        # The released snapshot is historical after rollback. Deleting its
        # manifest must not delete sources, vectors, or the active search state.
        before_space = await platform.get_space("pg-space")
        before_documents = {
            document.id: document for document in await platform.list_documents("pg-space")
        }
        before_index = await index_rows()
        deleted = await platform.delete_space_version("pg-space", first.id)
        assert deleted.deleted_at is not None
        assert [version.id for version in await platform.list_space_versions("pg-space")] == [base.id]
        assert await repository.get_space_version(first.id) == deleted
        assert await platform.get_space("pg-space") == before_space
        assert {
            document.id: document for document in await platform.list_documents("pg-space")
        } == before_documents
        assert await index_rows() == before_index
        # Historical read bindings survive snapshot soft deletion, and do not
        # consult active pointers or the deleted manifest row again.
        readonly = await platform.search("evidence", space="pg-space", snapshot=pinned,
            tuning=RetrievalTuning(vector_weight=0, rerank_enabled=False))
        assert readonly.trace.generation_id == base.generation_id

        # Concurrent first-result confirmation can seal a dimension only once.
        declared = await repository.create_generation(IndexGeneration(
            space_id=base.space_id, embedding_profile="test", retrieval_profile="test",
            identity_status="declared", build_spec=platform.current_build_spec(),
        ))
        sealed = await asyncio.gather(
            repository.confirm_generation_spec(declared.id, declared.build_spec.request_hash, 2),
            repository.confirm_generation_spec(declared.id, declared.build_spec.request_hash, 3),
            return_exceptions=True,
        )
        assert sum(isinstance(value, ValueError) for value in sealed) == 1
        confirmed = await repository.get_generation(declared.id)
        assert confirmed.identity_status == "verified"
        assert confirmed.build_spec.embedding.observed_dimensions in {2, 3}
        assert await repository.count_chunks() == len(before_index)
        await check(base)

        # Completed builds retain their audit reference and replay the exact
        # tombstone without restoring it or allocating another version number.
        replay = await platform.publish_space_build("pg-space", build.id)
        assert replay == deleted
        assert (await platform.get_space_build("pg-space", build.id)).published_version_id == first.id
        assert await platform.get_space("pg-space") == before_space
        assert (await platform.delete_space_version("pg-space", first.id)).deleted_at == deleted.deleted_at
        with pytest.raises(ValueError, match="current space version"):
            await platform.delete_space_version("pg-space", base.id)
        for stale_operation in (
            lambda: repository.activate_space_version(first),
            lambda: repository.update_space_version(first.model_copy(update={"note": "stale"})),
            lambda: platform.activate_space_version("pg-space", first.id),
            lambda: platform.set_space_version_note("pg-space", first.id, "hidden"),
        ):
            with pytest.raises(KeyError):
                await stale_operation()

        # A deleted highest historical number is never reused, even though the
        # application-facing list no longer contains it.
        saved = await platform.create_space_version("pg-space", "Number after deleted release")
        assert saved.version_number == first.version_number + 1
        await check(saved)

        # An already applied deletion remains idempotent after a new build
        # starts, while a fresh deletion is blocked until that build settles.
        busy = await platform.start_space_build("pg-space", "Deletion concurrency guard")
        assert (await platform.delete_space_version("pg-space", first.id)).deleted_at == deleted.deleted_at
        with pytest.raises(ValueError, match="staged build"):
            await platform.delete_space_version("pg-space", base.id)
        with pytest.raises(ValueError, match="active build"):
            await repository.delete_space_version(base.space_id, base.id)
        await check(saved)
        await platform.cancel_space_build("pg-space", busy.id)

        # Both requests target the same non-current version. The shared space
        # lock allows activation OR deletion, never an active tombstone.
        outcomes = await asyncio.gather(
            repository.activate_space_version(base),
            repository.delete_space_version(base.space_id, base.id),
            return_exceptions=True,
        )
        successes = [value for value in outcomes if not isinstance(value, BaseException)]
        failures = [value for value in outcomes if isinstance(value, BaseException)]
        assert len(successes) == len(failures) == 1
        assert isinstance(failures[0], (KeyError, ValueError))
        current = await platform.get_space("pg-space")
        active = await repository.get_space_version(current.active_version_id)
        assert active is not None and active.deleted_at is None
        await check(active)
        assert await index_rows() == before_index
        pending = await platform.submit(space="pg-space", title="Retained legacy pending source",
            content=b"retained legacy raw bytes", mime_type="text/plain")
        source_version = await repository.get_version(pending.version_id)
        running = await repository.claim_job(pending.id)
        with pytest.raises(ValueError, match="queued or failed"):
            await platform.cancel_job(running.id)
        legacy = running.model_copy(update={"generation_id": None, "build_request_hash": None})
        # Simulate pre-upgrade JSON only in this UUID-scoped disposable schema.
        async with repository.engine.begin() as connection:
            await connection.execute(update(jobs).where(jobs.c.id == legacy.id).values(
                payload=legacy.model_dump(mode="json")))
        preview = await repair_legacy_jobs(repository, "pg-space")
        assert preview["dry_run"] and len(preview["jobs"]) == 1
        assert (await repository.get_job(legacy.id)).status == "running"
        await repair_legacy_jobs(repository, "pg-space", confirm_no_workers=True)
        assert (await platform.cancel_job(legacy.id)).status == "cancelled"
        assert await repository.get_version(source_version.id) == source_version
        assert await platform.blob_store.get(source_version.blob_key) == b"retained legacy raw bytes"
        assert await index_rows() == before_index
        original = next(document for document in await platform.list_documents("pg-space")
                        if document.current_version_id is not None and document.status == "active")
        queued = await platform.submit(space="pg-space", title=original.title,
            source_id=original.source_id, content=b"changed evidence for atomic commit", mime_type="text/plain")
        original_clear = repository._clear_active_space_version

        async def fail_after_document_update(connection, space_id):
            raise RuntimeError("fault after version and document writes but before job completion")

        repository._clear_active_space_version = fail_after_document_update
        try:
            failed = await platform.process_job(queued.id)
            assert failed.status == "failed_terminal"
            # The transaction rolled back both earlier writes; the published
            # version remains READY, not the failed replacement's pointer.
            assert (await repository.get_document(original.id)).current_version_id == original.current_version_id
            assert (await repository.get_version(original.current_version_id)).status == "ready"
        finally:
            repository._clear_active_space_version = original_clear
        await platform.retry_job(queued.id)
        assert (await platform.process_job(queued.id)).status == "completed"
        assert (await repository.get_document(original.id)).current_version_id == queued.version_id
    finally:
        await platform.close()
        async with original_engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await original_engine.dispose()
