"""Create the v0.6.0 versioned knowledge platform.

Revision ID: 0001_knowledge_platform
Revises: None
"""

from alembic import op
from atlasflow.knowledge.infrastructure.postgres_repository import metadata

revision = "0001_knowledge_platform"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    metadata.create_all(bind=op.get_bind())
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_space_source_idx "
        "ON knowledge_documents (space_id, source_id)"
    )
    op.execute(
        "ALTER TABLE knowledge_chunks_v2 ADD COLUMN IF NOT EXISTS search_vector TSVECTOR "
        "GENERATED ALWAYS AS (to_tsvector('simple', lexical_text)) STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS knowledge_chunks_v2_search_idx "
        "ON knowledge_chunks_v2 USING GIN (search_vector)"
    )


def downgrade() -> None:
    # Knowledge migrations are intentionally forward-only. Archival/export must
    # happen before a separately approved destructive cleanup migration.
    pass
