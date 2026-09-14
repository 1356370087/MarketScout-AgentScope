"""Persist upstream Docling task ids for worker restart recovery.

Revision ID: 0006_docling_task_recovery
Revises: 0005_document_publish_pointer

The durable job row remembers the upstream conversion task so a restarted
worker polls the existing Docling task before resubmitting work (plan §KB-06
解析任务可靠性).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_docling_task_recovery"
down_revision: str | None = "0005_document_publish_pointer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the upstream task reference to document jobs."""
    op.execute(
        "ALTER TABLE research_document_jobs ADD COLUMN upstream_task_id varchar(128)"
    )
    op.execute(
        "CREATE INDEX ix_document_jobs_upstream_task ON research_document_jobs(upstream_task_id)"
    )


def downgrade() -> None:
    """Drop the upstream task reference."""
    op.execute(
        "DROP INDEX IF EXISTS ix_document_jobs_upstream_task"
    )
    op.execute(
        "ALTER TABLE research_document_jobs DROP COLUMN upstream_task_id"
    )
