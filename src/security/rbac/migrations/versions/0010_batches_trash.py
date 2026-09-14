"""Recycle-bin protection and batch operation ledgers (KB-14 / KB-15).

Revision ID: 0010_batches_trash
Revises: 0009_workspaces

``deleted_by``/``purge_after`` turn soft deletion into a 30-day recycle bin
with deferred physical cleanup; ``knowledge_batches``/``knowledge_batch_items``
track bulk operations per item so partial success never rolls back siblings.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_batches_trash"
down_revision: str | None = "0009_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add recycle-bin columns and batch tables."""
    op.execute(
        """ALTER TABLE research_documents
           ADD COLUMN deleted_by uuid,
           ADD COLUMN purge_after timestamptz"""
    )
    # Extend the status check to cover recycle-bin states.
    op.execute("ALTER TABLE research_documents DROP CONSTRAINT research_documents_status_check")
    op.execute(
        """ALTER TABLE research_documents
           ADD CONSTRAINT research_documents_status_check
           CHECK(status IN ('queued','processing','ready','failed','deleting',
                            'trashed','retained'))"""
    )
    op.execute(
        "CREATE INDEX ix_documents_purge_due ON research_documents(purge_after) "
        "WHERE deleted_at IS NOT NULL AND status='trashed'"
    )
    op.execute(
        """CREATE TABLE knowledge_batches (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          workspace_id uuid REFERENCES knowledge_workspaces(id),
          knowledge_base_id uuid REFERENCES knowledge_bases(id),
          operation varchar(48) NOT NULL,
          status varchar(24) NOT NULL DEFAULT 'pending'
            CONSTRAINT knowledge_batches_status_check
            CHECK(status IN ('pending','running','completed','cancelled','failed')),
          total_items integer NOT NULL DEFAULT 0,
          completed_count integer NOT NULL DEFAULT 0,
          failed_count integer NOT NULL DEFAULT 0,
          created_by uuid NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE INDEX ix_batches_creator ON knowledge_batches(created_by, created_at DESC)"
    )
    op.execute(
        """CREATE TABLE knowledge_batch_items (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          batch_id uuid NOT NULL REFERENCES knowledge_batches(id) ON DELETE CASCADE,
          document_id uuid NOT NULL,
          attempt integer NOT NULL DEFAULT 0,
          status varchar(24) NOT NULL DEFAULT 'pending'
            CONSTRAINT knowledge_batch_items_status_check
            CHECK(status IN ('pending','running','succeeded','failed','cancelled')),
          failure_code varchar(96),
          error_summary text,
          started_at timestamptz,
          finished_at timestamptz
        )"""
    )
    op.execute(
        "CREATE INDEX ix_batch_items_batch ON knowledge_batch_items(batch_id, status)"
    )


def downgrade() -> None:
    """Drop batch tables and recycle-bin columns."""
    op.execute("DROP TABLE IF EXISTS knowledge_batch_items")
    op.execute("DROP TABLE IF EXISTS knowledge_batches")
    op.execute("DROP INDEX IF EXISTS ix_documents_purge_due")
    op.execute("ALTER TABLE research_documents DROP CONSTRAINT research_documents_status_check")
    op.execute(
        """ALTER TABLE research_documents
           ADD CONSTRAINT research_documents_status_check
           CHECK(status IN ('queued','processing','ready','failed','deleting'))"""
    )
    op.execute(
        "ALTER TABLE research_documents "
        "DROP COLUMN IF EXISTS purge_after, DROP COLUMN IF EXISTS deleted_by"
    )
