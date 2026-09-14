"""Wiki pages, revisions and citations (KB-13).

Revision ID: 0013_wiki
Revises: 0012_facts

Pages hold structured Markdown blocks with stable IDs; each revision is
immutable with a ``base_revision`` optimistic lock. Citations link blocks
to document generations or fact assertions with staleness tracking.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_wiki"
down_revision: str | None = "0012_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create wiki page, revision and citation tables."""
    op.execute(
        """CREATE TABLE knowledge_pages (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
          title varchar(500) NOT NULL,
          template varchar(64) NOT NULL DEFAULT 'company_profile',
          entity_name varchar(200) NOT NULL DEFAULT '',
          current_revision_id uuid,
          created_by uuid,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE INDEX ix_pages_kb ON knowledge_pages(knowledge_base_id, updated_at DESC)"
    )
    op.execute(
        """CREATE TABLE knowledge_page_revisions (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          page_id uuid NOT NULL REFERENCES knowledge_pages(id) ON DELETE CASCADE,
          revision_number integer NOT NULL,
          blocks jsonb NOT NULL DEFAULT '[]',
          status varchar(24) NOT NULL DEFAULT 'draft'
            CONSTRAINT knowledge_page_revisions_status_check
            CHECK(status IN ('draft','published','rejected')),
          base_revision integer NOT NULL DEFAULT 0,
          created_by uuid,
          reviewed_by uuid,
          created_at timestamptz NOT NULL DEFAULT now(),
          published_at timestamptz,
          UNIQUE(page_id, revision_number)
        )"""
    )
    op.execute(
        "ALTER TABLE knowledge_pages ADD COLUMN IF NOT EXISTS current_revision_id uuid "
        "REFERENCES knowledge_page_revisions(id)"
    )
    op.execute(
        """CREATE TABLE knowledge_page_citations (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          revision_id uuid NOT NULL REFERENCES knowledge_page_revisions(id) ON DELETE CASCADE,
          block_id varchar(64) NOT NULL,
          citation_type varchar(24) NOT NULL DEFAULT 'document'
            CONSTRAINT knowledge_page_citations_type_check
            CHECK(citation_type IN ('document','fact')),
          document_id uuid REFERENCES research_documents(id) ON DELETE CASCADE,
          generation_id uuid REFERENCES research_document_generations(id) ON DELETE CASCADE,
          fact_assertion_id uuid REFERENCES knowledge_fact_assertions(id) ON DELETE CASCADE,
          citation_status varchar(32) NOT NULL DEFAULT 'current'
            CONSTRAINT knowledge_page_citations_status_check
            CHECK(citation_status IN ('current','source_changed','source_unavailable','needs_review')),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(revision_id, block_id, document_id, fact_assertion_id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_page_citations_revision ON knowledge_page_citations(revision_id)"
    )


def downgrade() -> None:
    """Drop wiki tables."""
    op.execute("DROP TABLE IF EXISTS knowledge_page_citations")
    op.execute("DROP TABLE IF EXISTS knowledge_page_revisions")
    op.execute("DROP INDEX IF EXISTS ix_pages_kb")
    op.execute("DROP TABLE IF EXISTS knowledge_pages")
