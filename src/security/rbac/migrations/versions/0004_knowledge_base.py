"""Add the knowledge-base unified material model on a cleaned documents DB.

Revision ID: 0004_knowledge_base
Revises: 0003_local_documents

Creates the KB-01..KB-03 structure: knowledge bases, collections, document
links, artifact versions, parse generations, structure units, retrieval
segments, competitive entities and audit/query ledgers. Existing document
tables are left untouched; upgrades refuse to run while pre-KB test data is
still present (run the documents cleanup command first).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_knowledge_base"
down_revision: str | None = "0003_local_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create knowledge-base tables after refusing uncleared legacy data."""
    bind = op.get_bind()
    leftovers = bind.execute(
        sa.text(
            """SELECT (SELECT count(*) FROM research_documents) AS documents,
                      (SELECT count(*) FROM research_run_sources) AS run_sources"""
        )
    ).fetchone()
    legacy_rows = (
        int(leftovers.documents) + int(leftovers.run_sources) if leftovers else 0
    )
    if legacy_rows:
        raise RuntimeError(
            f"legacy_document_data_not_purged:{legacy_rows} rows remain in the "
            "local-document tables. The knowledge-base upgrade intentionally "
            "refuses to migrate them; stop the document worker, verify no "
            "active research uses the test material, then run "
            "`uv run python -m open_deep_research.documents.cleanup --execute` "
            "and retry this migration."
        )

    op.execute(
        """CREATE TABLE knowledge_bases (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL,
          name varchar(200) NOT NULL, description text NOT NULL DEFAULT '',
          archived_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(owner_id, name)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_knowledge_bases_owner ON knowledge_bases(owner_id, updated_at DESC)"
    )
    op.execute(
        """CREATE TABLE knowledge_collections (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
          name varchar(200) NOT NULL, description text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(knowledge_base_id, name)
        )"""
    )
    op.execute(
        """CREATE TABLE knowledge_document_links (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
          collection_id uuid REFERENCES knowledge_collections(id) ON DELETE CASCADE,
          document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
          added_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    # NULL collection rows model a direct knowledge-base association; Postgres
    # treats NULLs as distinct, so both shapes need their own partial index.
    op.execute(
        """CREATE UNIQUE INDEX uq_kb_link_collection_doc
           ON knowledge_document_links(knowledge_base_id, collection_id, document_id)
           WHERE collection_id IS NOT NULL"""
    )
    op.execute(
        """CREATE UNIQUE INDEX uq_kb_link_direct_doc
           ON knowledge_document_links(knowledge_base_id, document_id)
           WHERE collection_id IS NULL"""
    )
    op.execute(
        "CREATE INDEX ix_kb_links_kb ON knowledge_document_links(knowledge_base_id)"
    )
    op.execute(
        "CREATE INDEX ix_kb_links_document ON knowledge_document_links(document_id)"
    )

    op.execute(
        """CREATE TABLE research_document_versions (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
          version_no integer NOT NULL CHECK(version_no > 0),
          filename varchar(240) NOT NULL, media_type varchar(160) NOT NULL,
          size_bytes bigint NOT NULL CHECK(size_bytes > 0), sha256 char(64) NOT NULL,
          storage_key text NOT NULL, note text NOT NULL DEFAULT '',
          supersedes_version_id uuid REFERENCES research_document_versions(id),
          uploaded_at timestamptz NOT NULL DEFAULT now(), UNIQUE(document_id, version_no)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_document_versions_document ON research_document_versions(document_id, version_no DESC)"
    )
    op.execute(
        """CREATE TABLE research_document_generations (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
          version_id uuid NOT NULL REFERENCES research_document_versions(id) ON DELETE CASCADE,
          status varchar(24) NOT NULL DEFAULT 'draft'
            CONSTRAINT research_document_generations_status_check
            CHECK(status IN ('draft','pending_review','published','rejected','withdrawn')),
          parse_config jsonb NOT NULL DEFAULT '{}',
          metadata_snapshot jsonb NOT NULL DEFAULT '{}',
          quality_report jsonb NOT NULL DEFAULT '{}',
          supersedes_generation_id uuid REFERENCES research_document_generations(id),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          published_at timestamptz, review_note text
        )"""
    )
    op.execute(
        "CREATE INDEX ix_document_generations_document ON research_document_generations(document_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_document_generations_status ON research_document_generations(status)"
    )
    op.execute(
        """CREATE TABLE research_document_units (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          generation_id uuid NOT NULL REFERENCES research_document_generations(id) ON DELETE CASCADE,
          ordinal integer NOT NULL, unit_type varchar(24) NOT NULL,
          parent_id uuid REFERENCES research_document_units(id),
          locator jsonb NOT NULL DEFAULT '{}',
          raw_text text NOT NULL DEFAULT '', revised_text text, index_text text NOT NULL DEFAULT '',
          attributes jsonb NOT NULL DEFAULT '{}',
          excluded boolean NOT NULL DEFAULT false, exclusion_reason text,
          UNIQUE(generation_id, ordinal)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_document_units_generation ON research_document_units(generation_id, ordinal)"
    )
    op.execute(
        """CREATE TABLE research_document_segments (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          generation_id uuid NOT NULL REFERENCES research_document_generations(id) ON DELETE CASCADE,
          unit_id uuid NOT NULL REFERENCES research_document_units(id) ON DELETE CASCADE,
          ordinal integer NOT NULL, index_text text NOT NULL,
          context_before text NOT NULL DEFAULT '', context_after text NOT NULL DEFAULT '',
          locator jsonb NOT NULL DEFAULT '{}',
          search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', index_text)) STORED,
          embedding vector(1536), embedding_model varchar(160), content_hash char(64) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(generation_id, ordinal)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_document_segments_generation ON research_document_segments(generation_id)"
    )
    op.execute(
        "CREATE INDEX ix_document_segments_fts ON research_document_segments USING gin(search_vector)"
    )
    op.execute(
        "CREATE INDEX ix_document_segments_trgm ON research_document_segments USING gin(index_text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_document_segments_hnsw ON research_document_segments USING hnsw(embedding vector_cosine_ops)"
    )

    op.execute(
        """CREATE TABLE knowledge_entities (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL,
          entity_kind varchar(16) NOT NULL
            CONSTRAINT knowledge_entities_kind_check CHECK(entity_kind IN ('company','product')),
          name_zh varchar(200), name_en varchar(200),
          parent_entity_id uuid REFERENCES knowledge_entities(id) ON DELETE RESTRICT,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT knowledge_entities_name_check CHECK(name_zh IS NOT NULL OR name_en IS NOT NULL)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_knowledge_entities_owner ON knowledge_entities(owner_id, entity_kind)"
    )
    op.execute(
        """CREATE TABLE knowledge_entity_aliases (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          entity_id uuid NOT NULL REFERENCES knowledge_entities(id) ON DELETE CASCADE,
          alias varchar(200) NOT NULL,
          alias_kind varchar(16) NOT NULL DEFAULT 'name'
            CONSTRAINT knowledge_entity_aliases_kind_check
            CHECK(alias_kind IN ('name','abbr','historical')),
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    # Deliberately not unique: one alias may legitimately match several
    # entities and the UI asks the user to disambiguate instead of merging.
    op.execute(
        "CREATE INDEX ix_knowledge_entity_aliases_alias ON knowledge_entity_aliases(alias)"
    )
    op.execute(
        """CREATE TABLE research_generation_entity_links (
          generation_id uuid NOT NULL REFERENCES research_document_generations(id) ON DELETE CASCADE,
          entity_id uuid NOT NULL REFERENCES knowledge_entities(id) ON DELETE CASCADE,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(generation_id, entity_id)
        )"""
    )
    op.execute(
        """CREATE TABLE research_document_operations (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL,
          document_id uuid REFERENCES research_documents(id) ON DELETE CASCADE,
          generation_id uuid REFERENCES research_document_generations(id) ON DELETE CASCADE,
          operation varchar(48) NOT NULL, actor_id uuid,
          reason text NOT NULL DEFAULT '', changes jsonb NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE INDEX ix_document_operations_document ON research_document_operations(document_id, created_at DESC)"
    )
    op.execute(
        """CREATE TABLE knowledge_queries (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL,
          query_text text NOT NULL, scope jsonb NOT NULL DEFAULT '{}',
          profile_version varchar(64), result_digest jsonb NOT NULL DEFAULT '{}',
          feedback_kind varchar(32), feedback_note text,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE INDEX ix_knowledge_queries_owner ON knowledge_queries(owner_id, created_at DESC)"
    )


def downgrade() -> None:
    """Drop the knowledge-base model; legacy document tables are untouched."""
    op.drop_table("knowledge_queries")
    op.drop_table("research_document_operations")
    op.drop_table("research_generation_entity_links")
    op.drop_table("knowledge_entity_aliases")
    op.drop_table("knowledge_entities")
    op.drop_index("ix_document_segments_hnsw", table_name="research_document_segments")
    op.drop_index("ix_document_segments_trgm", table_name="research_document_segments")
    op.drop_index("ix_document_segments_fts", table_name="research_document_segments")
    op.drop_index(
        "ix_document_segments_generation", table_name="research_document_segments"
    )
    op.drop_table("research_document_segments")
    op.drop_index("ix_document_units_generation", table_name="research_document_units")
    op.drop_table("research_document_units")
    op.drop_index(
        "ix_document_generations_status", table_name="research_document_generations"
    )
    op.drop_index(
        "ix_document_generations_document", table_name="research_document_generations"
    )
    op.drop_table("research_document_generations")
    op.drop_index(
        "ix_document_versions_document", table_name="research_document_versions"
    )
    op.drop_table("research_document_versions")
    op.drop_index("ix_kb_links_document", table_name="knowledge_document_links")
    op.drop_index("ix_kb_links_kb", table_name="knowledge_document_links")
    op.drop_index("uq_kb_link_direct_doc", table_name="knowledge_document_links")
    op.drop_index("uq_kb_link_collection_doc", table_name="knowledge_document_links")
    op.drop_table("knowledge_document_links")
    op.drop_table("knowledge_collections")
    op.drop_index("ix_knowledge_bases_owner", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
