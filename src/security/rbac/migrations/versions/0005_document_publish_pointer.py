"""Add the human-publish pointer and run source generation binding.

Revision ID: 0005_document_publish_pointer
Revises: 0004_knowledge_base

``research_documents.current_generation_id`` names the published parse
generation served by retrieval; ``research_run_sources.generation_id``
freezes the generation a Run actually bound at creation so later publishes
cannot drift an in-flight Run to new material.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_document_publish_pointer"
down_revision: str | None = "0004_knowledge_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the publish pointer and the run-source generation snapshot."""
    op.execute(
        """ALTER TABLE research_documents
           ADD COLUMN current_generation_id uuid
           REFERENCES research_document_generations(id)"""
    )
    op.execute(
        "CREATE INDEX ix_research_documents_current_generation ON research_documents(current_generation_id)"
    )
    op.execute(
        """ALTER TABLE research_run_sources
           ADD COLUMN generation_id uuid REFERENCES research_document_generations(id)"""
    )


def downgrade() -> None:
    """Drop the publish pointer and run-source generation snapshot."""
    op.execute("ALTER TABLE research_run_sources DROP COLUMN generation_id")
    op.execute(
        "DROP INDEX IF EXISTS ix_research_documents_current_generation"
    )
    op.execute("ALTER TABLE research_documents DROP COLUMN current_generation_id")
