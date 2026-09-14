"""Web sync sources, content fingerprints and source relations (KB-10/KB-11).

Revision ID: 0011_sync_dedup
Revises: 0010_batches_trash

Sync sources bind a URL to a logical document with conditional-request
state (ETag / Last-Modified / content hash) and a single-pending-draft
guard. Content fingerprints store normalized-text and shingled hashes for
three-layer duplicate detection; source relations record confirmed or
suspected duplicates between documents without auto-merging.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_sync_dedup"
down_revision: str | None = "0010_batches_trash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create sync-source, fingerprint and source-relation tables."""
    op.execute(
        """CREATE TABLE knowledge_sync_sources (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
          document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
          input_url text NOT NULL,
          normalized_url text NOT NULL,
          final_url text,
          refresh_mode varchar(16) NOT NULL DEFAULT 'daily'
            CONSTRAINT knowledge_sync_sources_refresh_check
            CHECK(refresh_mode IN ('manual','daily','weekly')),
          etag varchar(500),
          last_modified varchar(200),
          content_hash char(64),
          last_success_at timestamptz,
          next_run_at timestamptz,
          consecutive_failures integer NOT NULL DEFAULT 0,
          last_fetched_version_id uuid,
          last_published_version_id uuid,
          paused boolean NOT NULL DEFAULT false,
          created_by uuid,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(document_id, normalized_url)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_sync_sources_due ON knowledge_sync_sources(next_run_at) "
        "WHERE NOT paused"
    )
    op.execute(
        """CREATE TABLE knowledge_content_fingerprints (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
          generation_id uuid NOT NULL REFERENCES research_document_generations(id) ON DELETE CASCADE,
          normalized_text_hash char(64) NOT NULL,
          shingle_hashes text[] NOT NULL DEFAULT '{}',
          source_class varchar(32) NOT NULL DEFAULT 'unknown'
            CONSTRAINT knowledge_fingerprints_class_check
            CHECK(source_class IN ('official','media','repost','internal_interview','other','unknown')),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(generation_id, normalized_text_hash)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_fingerprints_normalized ON knowledge_content_fingerprints(normalized_text_hash)"
    )
    op.execute(
        """CREATE TABLE knowledge_source_relations (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          workspace_id uuid,
          left_document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
          right_document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
          relation_type varchar(32) NOT NULL
            CONSTRAINT knowledge_source_relations_type_check
            CHECK(relation_type IN ('exact_duplicate','near_duplicate','repost_of','copied_from')),
          similarity numeric(4,3),
          evidence jsonb NOT NULL DEFAULT '{}',
          confirmed_by uuid,
          confirmed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(left_document_id, right_document_id, relation_type),
          CONSTRAINT knowledge_source_relations_order CHECK(left_document_id <> right_document_id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_source_relations_left ON knowledge_source_relations(left_document_id)"
    )


def downgrade() -> None:
    """Drop sync, fingerprint and relation tables."""
    op.execute("DROP TABLE IF EXISTS knowledge_source_relations")
    op.execute("DROP TABLE IF EXISTS knowledge_content_fingerprints")
    op.execute("DROP INDEX IF EXISTS ix_sync_sources_due")
    op.execute("DROP TABLE IF EXISTS knowledge_sync_sources")
