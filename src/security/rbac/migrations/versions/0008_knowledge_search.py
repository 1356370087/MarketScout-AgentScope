"""Knowledge search profiles, saved searches and evaluation ledgers.

Revision ID: 0008_knowledge_search
Revises: 0007_quality_workbench

Search parameters are versioned as ``knowledge_search_profiles`` (one row per
version, a seeded default) so the evaluation bench can compare configurations;
``knowledge_saved_searches`` are reusable conditions, not snapshots. Stage-6
evaluation gets ``knowledge_eval_sets`` / ``knowledge_eval_runs``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_knowledge_search"
down_revision: str | None = "0007_quality_workbench"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_PROFILE = {
    "candidate_limit_per_route": 40,
    "rerank_candidates": 30,
    "result_limit": 12,
    "per_document_quota": 3,
    "context_neighbors": 1,
    "context_char_budget": 2400,
    "rerank_min_score": 2,
}


def upgrade() -> None:
    """Create search-profile, saved-search and evaluation tables."""
    op.execute(
        """CREATE TABLE knowledge_search_profiles (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          version varchar(64) NOT NULL UNIQUE,
          parameters jsonb NOT NULL DEFAULT '{}',
          is_default boolean NOT NULL DEFAULT false,
          note text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE INDEX ix_search_profiles_default ON knowledge_search_profiles(is_default, created_at DESC)"
    )
    op.get_bind().execute(
        sa.text(
            """INSERT INTO knowledge_search_profiles(version, parameters, is_default, note)
               VALUES ('v1-default', CAST(:params AS jsonb), true, '首版检索参数（KB-05）')"""
        ),
        {"params": json.dumps(_DEFAULT_PROFILE, ensure_ascii=False)},
    )
    op.execute(
        """CREATE TABLE knowledge_saved_searches (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_id uuid NOT NULL,
          name varchar(200) NOT NULL,
          query_text text NOT NULL DEFAULT '',
          scope jsonb NOT NULL DEFAULT '{}',
          filters jsonb NOT NULL DEFAULT '{}',
          version_mode varchar(24) NOT NULL DEFAULT 'current',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(owner_id, name)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_saved_searches_owner ON knowledge_saved_searches(owner_id, updated_at DESC)"
    )
    op.execute(
        """CREATE TABLE knowledge_eval_sets (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          name varchar(200) NOT NULL UNIQUE,
          items jsonb NOT NULL DEFAULT '[]',
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        """CREATE TABLE knowledge_eval_runs (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          set_id uuid NOT NULL REFERENCES knowledge_eval_sets(id) ON DELETE CASCADE,
          owner_id uuid NOT NULL,
          profile_version varchar(64),
          metrics jsonb NOT NULL DEFAULT '{}',
          details jsonb NOT NULL DEFAULT '{}',
          started_at timestamptz NOT NULL DEFAULT now(),
          finished_at timestamptz
        )"""
    )
    op.execute(
        "CREATE INDEX ix_eval_runs_set ON knowledge_eval_runs(set_id, started_at DESC)"
    )


def downgrade() -> None:
    """Drop search and evaluation tables."""
    op.drop_table("knowledge_eval_runs")
    op.drop_table("knowledge_eval_sets")
    op.drop_table("knowledge_saved_searches")
    op.drop_index("ix_search_profiles_default", table_name="knowledge_search_profiles")
    op.drop_table("knowledge_search_profiles")
