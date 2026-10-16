"""Execution journal/outbox schema. Official saver tables are explicitly initialized.

Revision ID: 0004_execution
Revises: 0003_space_builds
"""
from alembic import op
from atlasflow.execution.postgres import execution_tables
from sqlalchemy.schema import CreateSchema

revision = "0004_execution"
down_revision = "0003_space_builds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(CreateSchema("atlasflow_execution", if_not_exists=True))
    metadata, _ = execution_tables()
    metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Preserve checkpoints, deleted tombstones and audit records. Never silently
    # revert recoverable Runs to an in-memory execution implementation.
    pass
