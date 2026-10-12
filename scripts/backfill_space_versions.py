"""Reconstruct whole-space snapshots from retained document-version/index records.

Preview is read-only. --apply inserts snapshots and points each space at its
current snapshot, without changing document pointers or reindexing. Historical
snapshots are labelled as reconstructions, not backdated original checkpoints.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from atlasflow.config import Settings
from atlasflow.knowledge.domain import (
    DocumentStatus,
    DocumentVersion,
    KnowledgeDocument,
    KnowledgeSpace,
    KnowledgeSpaceVersion,
    SpaceDocumentState,
    VersionStatus,
)
from atlasflow.knowledge.infrastructure.postgres_repository import (
    chunks,
    documents,
    generations,
    jobs,
    space_versions,
    spaces,
    versions,
)
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine


async def reconstruct(
    connection: AsyncConnection, slug: str
) -> tuple[KnowledgeSpace, list[KnowledgeSpaceVersion]]:
    space_row = (
        await connection.execute(
            select(spaces.c.payload).where(spaces.c.slug == slug).with_for_update()
        )
    ).first()
    if space_row is None:
        raise ValueError(f"space not found: {slug}")
    space = KnowledgeSpace.model_validate(space_row.payload)
    existing = (
        await connection.execute(
            select(space_versions.c.id).where(space_versions.c.space_id == space.id).limit(1)
        )
    ).first()
    if existing or space.active_version_id:
        raise ValueError(f"{slug}: space versions already exist; refusing to overwrite them")
    busy = (
        await connection.execute(
            select(jobs.c.id).where(
                jobs.c.space_id == space.id,
                jobs.c.status.in_(["queued", "running", "failed_retryable"]),
            ).limit(1)
        )
    ).first()
    if busy:
        raise ValueError(f"{slug}: ingestion is still active")

    document_rows = (
        await connection.execute(
            select(documents.c.payload).where(documents.c.space_id == space.id).with_for_update()
        )
    ).all()
    current_documents = [KnowledgeDocument.model_validate(row.payload) for row in document_rows]
    if not current_documents or any(doc.status != DocumentStatus.ACTIVE for doc in current_documents):
        raise ValueError(f"{slug}: cannot infer historical manifests with no or archived documents")

    by_document: dict[str, list[DocumentVersion]] = {}
    for doc in current_documents:
        rows = (
            await connection.execute(
                select(versions.c.payload).where(versions.c.document_id == doc.id)
            )
        ).all()
        ordered = sorted(
            (DocumentVersion.model_validate(row.payload) for row in rows),
            key=lambda version: version.version_number,
        )
        if [version.version_number for version in ordered] != list(range(1, len(ordered) + 1)):
            raise ValueError(f"{slug}: document {doc.id} has incomplete version history")
        if not ordered or any(
            version.status != VersionStatus.READY or version.indexed_at is None
            for version in ordered
        ):
            raise ValueError(f"{slug}: document {doc.id} has an unready version")
        by_document[doc.id] = ordered

    wave_counts = {len(history) for history in by_document.values()}
    if len(wave_counts) != 1:
        raise ValueError(f"{slug}: documents do not share complete version waves")
    wave_count = wave_counts.pop()
    if any(doc.current_version_id != by_document[doc.id][-1].id for doc in current_documents):
        raise ValueError(f"{slug}: current document pointers do not match the latest wave")

    version_ids = [version.id for history in by_document.values() for version in history]
    index_rows = (
        await connection.execute(
            select(
                chunks.c.version_id,
                chunks.c.generation_id,
                func.count().label("total"),
                func.count(chunks.c.embedding).label("embedded"),
            )
            .where(chunks.c.version_id.in_(version_ids), chunks.c.chunk_kind == "child")
            .group_by(chunks.c.version_id, chunks.c.generation_id)
        )
    ).all()
    index_by_version: dict[str, list[tuple[str, int, int]]] = {}
    for row in index_rows:
        index_by_version.setdefault(row.version_id, []).append(
            (row.generation_id, row.total, row.embedded)
        )

    now = datetime.now(UTC)
    snapshots: list[KnowledgeSpaceVersion] = []
    for wave in range(wave_count):
        selected = {doc.id: by_document[doc.id][wave] for doc in current_documents}
        parser_chunker = {
            (version.parser_version, version.chunker_version) for version in selected.values()
        }
        if len(parser_chunker) != 1:
            raise ValueError(f"{slug}: parser/chunker versions differ within wave {wave + 1}")
        wave_generations: set[str] = set()
        for version in selected.values():
            matches = index_by_version.get(version.id, [])
            if len(matches) != 1 or matches[0][1] == 0 or matches[0][1] != matches[0][2]:
                raise ValueError(f"{slug}: incomplete child index for {version.id}")
            wave_generations.add(matches[0][0])
        if len(wave_generations) != 1:
            raise ValueError(f"{slug}: mixed index generations in wave {wave + 1}")
        generation_id = wave_generations.pop()
        generation_exists = (
            await connection.execute(
                select(generations.c.id).where(
                    generations.c.id == generation_id,
                    generations.c.space_id == space.id,
                )
            )
        ).first()
        if not generation_exists:
            raise ValueError(f"{slug}: missing index generation {generation_id}")
        parser, chunker = parser_chunker.pop()
        indexed_dates = {version.indexed_at.date().isoformat() for version in selected.values()}
        if len(indexed_dates) != 1:
            raise ValueError(f"{slug}: versions in wave {wave + 1} were indexed on different days")
        indexed_date = indexed_dates.pop()
        note = (
            f"依据现存 {len(selected)} 份文档 v{wave + 1}、完整 child 索引回填"
            f"（{parser} / {chunker}；原索引日期 {indexed_date}）。"
            "这是事后重建的清单，不是当时保存的快照。"
        )
        snapshots.append(
            KnowledgeSpaceVersion(
                space_id=space.id,
                version_number=wave + 1,
                generation_id=generation_id,
                documents={
                    doc_id: SpaceDocumentState(version_id=version.id, status=DocumentStatus.ACTIVE)
                    for doc_id, version in selected.items()
                },
                note=note,
                created_at=now,
                activated_at=now if wave == wave_count - 1 else None,
            )
        )
    if snapshots[-1].generation_id != space.active_generation_id:
        raise ValueError(f"{slug}: latest index generation is not the active generation")
    return space, snapshots


async def run(slugs: list[str], apply: bool) -> None:
    engine = create_async_engine(Settings().knowledge_database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            plans = [await reconstruct(connection, slug) for slug in slugs]
            for (space, snapshots), slug in zip(plans, slugs, strict=True):
                if not apply:
                    continue
                for version in snapshots:
                    await connection.execute(space_versions.insert().values(
                        id=version.id,
                        space_id=version.space_id,
                        version_number=version.version_number,
                        payload=version.model_dump(mode="json"),
                        created_at=version.created_at,
                    ))
                latest = snapshots[-1]
                updated = space.model_copy(update={
                    "active_version_id": latest.id,
                    "updated_at": datetime.now(UTC),
                })
                await connection.execute(
                    update(spaces).where(spaces.c.id == space.id).values(
                        payload=updated.model_dump(mode="json")
                    )
                )
        for (_, snapshots), slug in zip(plans, slugs, strict=True):
            print(json.dumps({
                "space": slug,
                "action": "inserted" if apply else "preview",
                "versions": [
                    {
                        "number": version.version_number,
                        "document_count": len(version.documents),
                        "generation_id": version.generation_id,
                        "note": version.note,
                    }
                    for version in snapshots
                ],
            }, ensure_ascii=False))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--space", action="append", required=True, help="Space slug; repeat as needed")
    parser.add_argument("--apply", action="store_true", help="Insert all verified snapshots atomically")
    args = parser.parse_args()
    if len(args.space) != len(set(args.space)):
        parser.error("duplicate --space value")
    asyncio.run(run(args.space, args.apply))


if __name__ == "__main__":
    main()
