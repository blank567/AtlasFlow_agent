"""Add whole-space version manifests.

Revision ID: 0002_space_versions
Revises: 0001_knowledge_platform
"""

from alembic import op
from atlasflow.knowledge.infrastructure.postgres_repository import space_versions

revision = "0002_space_versions"
down_revision = "0001_knowledge_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    space_versions.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Historical space manifests are intentionally retained; cleanup needs explicit approval.
    pass
