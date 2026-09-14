"""Quality-bench state: revision counters, reparse scope, reparse jobs.

Revision ID: 0007_quality_workbench
Revises: 0006_docling_task_recovery

``revision`` is the optimistic-lock counter that stops two browser tabs from
overwriting each other's corrections; ``reparse_scope`` carries the pages or
sheets a scoped re-parse should replace. The job-kind check gains
``reparse`` so the durable queue can carry scoped work.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_quality_workbench"
down_revision: str | None = "0006_docling_task_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add workbench columns and the reparse job kind."""
    op.execute(
        """ALTER TABLE research_document_generations
           ADD COLUMN revision integer NOT NULL DEFAULT 0,
           ADD COLUMN reparse_scope jsonb"""
    )
    op.execute(
        "ALTER TABLE research_document_jobs DROP CONSTRAINT research_document_jobs_kind_check"
    )
    op.execute(
        """ALTER TABLE research_document_jobs
           ADD CONSTRAINT research_document_jobs_kind_check
           CHECK(kind IN ('ingest','delete','reindex','reparse'))"""
    )


def downgrade() -> None:
    """Revert workbench columns and the reparse job kind."""
    op.execute(
        "DELETE FROM research_document_jobs WHERE kind='reparse'"
    )
    op.execute(
        "ALTER TABLE research_document_jobs DROP CONSTRAINT research_document_jobs_kind_check"
    )
    op.execute(
        """ALTER TABLE research_document_jobs
           ADD CONSTRAINT research_document_jobs_kind_check
           CHECK(kind IN ('ingest','delete','reindex'))"""
    )
    op.execute(
        "ALTER TABLE research_document_generations DROP COLUMN reparse_scope"
    )
    op.execute(
        "ALTER TABLE research_document_generations DROP COLUMN revision"
    )
