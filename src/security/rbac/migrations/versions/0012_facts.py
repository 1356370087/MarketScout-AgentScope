"""Fact ledger: keys, assertions and evidence (KB-12).

Revision ID: 0012_facts
Revises: 0011_sync_dedup

A fact key identifies the comparison axis (entity × metric × region × period
× condition); assertions carry the value with unit/currency/scale/period and
link to document evidence via generation + unit/segment. Multiple conflicting
values coexist on the same key; published assertions are immutable and any
change creates a new assertion linked to the one it supersedes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_facts"
down_revision: str | None = "0011_sync_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create fact-key, assertion and evidence tables."""
    op.execute(
        """CREATE TABLE knowledge_fact_keys (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          workspace_id uuid REFERENCES knowledge_workspaces(id),
          entity_name varchar(200) NOT NULL,
          metric varchar(200) NOT NULL,
          region varchar(100) NOT NULL DEFAULT '',
          period_label varchar(100) NOT NULL DEFAULT '',
          condition_text varchar(500) NOT NULL DEFAULT '',
          UNIQUE(workspace_id, entity_name, metric, region, period_label, condition_text)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_fact_keys_lookup ON knowledge_fact_keys(entity_name, metric)"
    )
    op.execute(
        """CREATE TABLE knowledge_fact_assertions (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
          fact_key_id uuid NOT NULL REFERENCES knowledge_fact_keys(id) ON DELETE CASCADE,
          value_text text NOT NULL DEFAULT '',
          value_numeric numeric(28,10),
          unit varchar(64) NOT NULL DEFAULT '',
          currency varchar(8) NOT NULL DEFAULT '',
          scale varchar(32) NOT NULL DEFAULT '',
          data_period varchar(100) NOT NULL DEFAULT '',
          valid_from date,
          valid_until date,
          condition_text varchar(500) NOT NULL DEFAULT '',
          raw_statement text NOT NULL DEFAULT '',
          normalized_result text NOT NULL DEFAULT '',
          value_origin varchar(24) NOT NULL DEFAULT 'source'
            CONSTRAINT knowledge_fact_assertions_origin_check
            CHECK(value_origin IN ('source','formula','model_inferred')),
          status varchar(24) NOT NULL DEFAULT 'draft'
            CONSTRAINT knowledge_fact_assertions_status_check
            CHECK(status IN ('draft','pending_review','published','rejected','withdrawn')),
          verification varchar(24) NOT NULL DEFAULT 'unverified'
            CONSTRAINT knowledge_fact_assertions_verification_check
            CHECK(verification IN ('unverified','verified','disputed')),
          supersedes_id uuid REFERENCES knowledge_fact_assertions(id),
          extraction_key varchar(200),
          created_by uuid,
          reviewed_by uuid,
          review_note text,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          published_at timestamptz
        )"""
    )
    op.execute(
        "CREATE INDEX ix_fact_assertions_key ON knowledge_fact_assertions(fact_key_id, status)"
    )
    op.execute(
        "CREATE INDEX ix_fact_assertions_extraction ON knowledge_fact_assertions(extraction_key)"
    )
    op.execute(
        """CREATE TABLE knowledge_fact_evidence (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          assertion_id uuid NOT NULL REFERENCES knowledge_fact_assertions(id) ON DELETE CASCADE,
          document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
          generation_id uuid NOT NULL REFERENCES research_document_generations(id) ON DELETE CASCADE,
          unit_id uuid REFERENCES research_document_units(id) ON DELETE CASCADE,
          segment_id uuid REFERENCES research_document_segments(id) ON DELETE CASCADE,
          excerpt text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(assertion_id, generation_id, segment_id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_fact_evidence_assertion ON knowledge_fact_evidence(assertion_id)"
    )


def downgrade() -> None:
    """Drop fact tables."""
    op.execute("DROP TABLE IF EXISTS knowledge_fact_evidence")
    op.execute("DROP INDEX IF EXISTS ix_fact_assertions_extraction")
    op.execute("DROP INDEX IF EXISTS ix_fact_assertions_key")
    op.execute("DROP TABLE IF EXISTS knowledge_fact_assertions")
    op.execute("DROP INDEX IF EXISTS ix_fact_keys_lookup")
    op.execute("DROP TABLE IF EXISTS knowledge_fact_keys")
