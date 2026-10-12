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
from atlasflow.knowledge.domain import KnowledgeDocument, RetrievalTuning
from atlasflow.knowledge.infrastructure import MemoryBlobStore
from atlasflow.knowledge.infrastructure.postgres_repository import (
    PostgresKnowledgeRepository,
    metadata,
)
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.providers import RerankResult
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
    finally:
        await platform.close()
        async with original_engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await original_engine.dispose()
