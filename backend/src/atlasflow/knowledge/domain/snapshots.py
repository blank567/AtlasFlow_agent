"""Build a binding from one repository transaction, never from a later active pointer."""

from atlasflow.knowledge.domain.index_specs import SearchSnapshot, SnapshotDocument
from atlasflow.knowledge.domain.models import (
    IndexGeneration,
    KnowledgeDocument,
    KnowledgeSpace,
    KnowledgeSpaceVersion,
)


def make_snapshot(
    space: KnowledgeSpace, documents: list[KnowledgeDocument],
    generation: IndexGeneration | None, version: KnowledgeSpaceVersion | None = None,
) -> SearchSnapshot:
    if version is not None and (version.space_id != space.id or version.deleted_at is not None):
        raise KeyError("knowledge space version not found")
    manifest = (
        {key: SnapshotDocument(version_id=item.version_id, status=item.status)
         for key, item in version.documents.items()}
        if version else
        {item.id: SnapshotDocument(version_id=item.current_version_id, status=item.status)
         for item in documents}
    )
    generation_id = version.generation_id if version else space.active_generation_id
    if generation_id and (generation is None or generation.id != generation_id
                          or generation.space_id != space.id):
        raise ValueError("SEARCH_SNAPSHOT_DATA_MISSING: index generation is unavailable")
    return SearchSnapshot(
        space_id=space.id, space_slug=space.slug,
        space_version_id=version.id if version else space.active_version_id,
        generation_id=generation_id, documents=manifest,
        identity_status=generation.identity_status if generation else "legacy_unknown",
        build_spec=generation.build_spec if generation else None,
    )


def check_snapshot_generation(snapshot: SearchSnapshot, generation: IndexGeneration | None) -> None:
    if snapshot.generation_id is None:
        return
    if (generation is None or generation.space_id != snapshot.space_id
            or generation.build_spec != snapshot.build_spec
            or generation.identity_status != snapshot.identity_status):
        raise ValueError("SEARCH_SNAPSHOT_DATA_MISSING: pinned generation identity changed or is unavailable")
