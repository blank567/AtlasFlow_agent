"""Add staged whole-space builds without changing published index pointers.

Revision ID: 0003_space_builds
Revises: 0002_space_versions
"""

from alembic import op
from atlasflow.knowledge.infrastructure.postgres_repository import space_builds

revision = "0003_space_builds"
down_revision = "0002_space_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    space_builds.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Retain staged and historical records; destructive cleanup is a separate operation.
    pass
