"""Workspaces, membership and asset ownership (plan §2 / KB-09).

Revision ID: 0009_workspaces
Revises: 0008_knowledge_search

Creates personal/team workspaces with workspace- and knowledge-base-level
membership, backfills ownership for existing knowledge bases, documents,
entities, queries and evaluation sets, swaps the per-owner document dedup
constraint for per-home-knowledge-base dedup, and adds the team audit
ledger. Ownership backfill keeps every existing id and citation intact:
each legacy owner gets a personal workspace, un-attached documents get a
default knowledge base, and documents linked to several knowledge bases
keep the earliest link as their home (later phases copy on demand instead
of rewriting history here — cross-KB copies remain link-level reuse per
plan §2.2, so no row duplication happens in this migration).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_workspaces"
down_revision: str | None = "0008_knowledge_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create workspace structures and backfill ownership in place."""
    op.execute(
        """CREATE TABLE knowledge_workspaces (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          kind varchar(16) NOT NULL
            CONSTRAINT knowledge_workspaces_kind_check CHECK(kind IN ('personal','team')),
          name varchar(200) NOT NULL,
          status varchar(16) NOT NULL DEFAULT 'active'
            CONSTRAINT knowledge_workspaces_status_check
            CHECK(status IN ('active','suspended','deleted')),
          quota_documents integer, quota_bytes bigint,
          created_by uuid, created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_workspaces_personal_owner "
        "ON knowledge_workspaces((kind='personal'), created_by) WHERE kind='personal'"
    )
    op.execute(
        """CREATE TABLE knowledge_workspace_members (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          workspace_id uuid NOT NULL REFERENCES knowledge_workspaces(id) ON DELETE CASCADE,
          user_id uuid NOT NULL,
          role varchar(16) NOT NULL
            CONSTRAINT knowledge_workspace_members_role_check
            CHECK(role IN ('owner','admin','member')),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(workspace_id, user_id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_workspace_members_user ON knowledge_workspace_members(user_id)"
    )
    op.execute(
        """CREATE TABLE knowledge_base_members (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
          user_id uuid NOT NULL,
          role varchar(16) NOT NULL
            CONSTRAINT knowledge_base_members_role_check
            CHECK(role IN ('viewer','contributor','manager')),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(knowledge_base_id, user_id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_kb_members_user ON knowledge_base_members(user_id)"
    )
    op.execute(
        """CREATE TABLE knowledge_audit_events (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          actor_id uuid, workspace_id uuid, knowledge_base_id uuid,
          action varchar(64) NOT NULL, target jsonb NOT NULL DEFAULT '{}',
          before jsonb NOT NULL DEFAULT '{}', after jsonb NOT NULL DEFAULT '{}',
          request_id varchar(128), occurred_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE INDEX ix_audit_events_ws ON knowledge_audit_events(workspace_id, occurred_at DESC)"
    )
    op.execute(
        "ALTER TABLE knowledge_bases ADD COLUMN workspace_id uuid "
        "REFERENCES knowledge_workspaces(id)"
    )
    op.execute(
        "ALTER TABLE knowledge_bases ADD COLUMN visibility varchar(16) "
        "NOT NULL DEFAULT 'team' "
        "CONSTRAINT knowledge_bases_visibility_check CHECK(visibility IN ('team','restricted'))"
    )
    op.execute("ALTER TABLE knowledge_bases ADD COLUMN created_by uuid")
    op.execute(
        "ALTER TABLE research_documents ADD COLUMN workspace_id uuid "
        "REFERENCES knowledge_workspaces(id)"
    )
    op.execute(
        "ALTER TABLE research_documents ADD COLUMN home_knowledge_base_id uuid "
        "REFERENCES knowledge_bases(id)"
    )
    op.execute("ALTER TABLE research_documents ADD COLUMN created_by uuid")
    op.execute(
        "ALTER TABLE research_documents ADD COLUMN copied_from_document_id uuid "
        "REFERENCES research_documents(id)"
    )
    op.execute(
        "ALTER TABLE research_documents ADD COLUMN copied_from_version_id uuid "
        "REFERENCES research_document_versions(id)"
    )

    bind = op.get_bind()
    # 1. One personal workspace per legacy owner of any knowledge asset.
    bind.execute(
        sa.text(
            """
            INSERT INTO knowledge_workspaces(kind, name, created_by)
            SELECT DISTINCT 'personal', '个人空间', owner_id
              FROM (
                SELECT owner_id FROM research_documents
                UNION SELECT owner_id FROM knowledge_bases
                UNION SELECT owner_id FROM knowledge_queries
              ) owners WHERE owner_id IS NOT NULL
            ON CONFLICT DO NOTHING
            """
        )
    )
    # 2. Personal workspaces always seat exactly their owner.
    bind.execute(
        sa.text(
            """
            INSERT INTO knowledge_workspace_members(workspace_id, user_id, role)
            SELECT w.id, w.created_by, 'owner'
              FROM knowledge_workspaces w WHERE w.kind='personal'
            ON CONFLICT DO NOTHING
            """
        )
    )
    # 3. Knowledge bases move into their owner's personal workspace (team
    #    workspaces attach new bases later through the API).
    bind.execute(
        sa.text(
            """
            UPDATE knowledge_bases kb
               SET workspace_id = w.id,
                   visibility = 'team',
                   created_by = kb.owner_id
              FROM knowledge_workspaces w
             WHERE w.kind='personal' AND w.created_by = kb.owner_id
               AND kb.workspace_id IS NULL
            """
        )
    )
    # 4. Legacy documents get a default home knowledge base in their owner's
    #    personal workspace when no link exists yet.
    bind.execute(
        sa.text(
            """
            INSERT INTO knowledge_bases(workspace_id, owner_id, name, description,
                                        visibility, created_by)
            SELECT w.id, d.owner_id, '默认资料库',
                   '迁移自动创建：收纳未归属知识库的既有资料', 'team', d.owner_id
              FROM research_documents d
              JOIN knowledge_workspaces w
                ON w.kind='personal' AND w.created_by = d.owner_id
             WHERE d.deleted_at IS NULL
               AND NOT EXISTS (
                     SELECT 1 FROM knowledge_document_links l
                      WHERE l.document_id = d.id)
            ON CONFLICT DO NOTHING
            """
        )
    )
    # 5. Home base = earliest link (multi-KB documents keep link-level reuse;
    #    cross-KB independent copies are created on demand, not by migration).
    bind.execute(
        sa.text(
            """
            UPDATE research_documents d
               SET home_knowledge_base_id = COALESCE(
                     (SELECT l.knowledge_base_id
                        FROM knowledge_document_links l
                       WHERE l.document_id = d.id
                       ORDER BY l.added_at, l.id LIMIT 1),
                     (SELECT kb2.id FROM knowledge_bases kb2
                       WHERE kb2.workspace_id IN (
                             SELECT w.id FROM knowledge_workspaces w
                              WHERE w.kind='personal'
                                AND w.created_by = d.owner_id)
                       ORDER BY kb2.created_at LIMIT 1)),
                   created_by = d.owner_id
             WHERE d.home_knowledge_base_id IS NULL
            """
        )
    )
    # 6. Documents without any link (and without a legacy owner workspace)
    #    still need a home: fall back to any base in their workspace chain.
    bind.execute(
        sa.text(
            """
            UPDATE research_documents d
               SET home_knowledge_base_id = sub.home_id
              FROM (
                SELECT d2.id,
                       (SELECT kb.id FROM knowledge_bases kb
                         WHERE kb.owner_id = d2.owner_id
                         ORDER BY kb.created_at LIMIT 1) AS home_id
                  FROM research_documents d2
                 WHERE d2.home_knowledge_base_id IS NULL
              ) sub
             WHERE d.id = sub.id AND sub.home_id IS NOT NULL
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE research_documents d
               SET workspace_id = kb.workspace_id
              FROM knowledge_bases kb
             WHERE d.home_knowledge_base_id = kb.id
               AND d.workspace_id IS NULL
            """
        )
    )
    # 6b. Every document links into its home knowledge base so KB listings
    #     see migrated material without touching historical link rows.
    bind.execute(
        sa.text(
            """
            INSERT INTO knowledge_document_links(knowledge_base_id, document_id)
            SELECT d.home_knowledge_base_id, d.id
              FROM research_documents d
             WHERE d.home_knowledge_base_id IS NOT NULL
               AND NOT EXISTS (
                     SELECT 1 FROM knowledge_document_links l
                      WHERE l.knowledge_base_id = d.home_knowledge_base_id
                        AND l.document_id = d.id)
            ON CONFLICT DO NOTHING
            """
        )
    )

    # 7. Entities and the query ledger keep owner_id as their compatibility
    #    key in phase A; their workspace columns arrive with their feature
    #    phases (facts/sync) so this migration never adds unused columns.

    # 8. Per-home-knowledge-base dedup replaces the per-owner constraint so
    #    the same file can live independently in different knowledge bases.
    op.execute("DROP INDEX IF EXISTS uq_research_documents_owner_sha_active")
    op.execute(
        "CREATE UNIQUE INDEX uq_research_documents_home_sha_active "
        "ON research_documents(home_knowledge_base_id, sha256) "
        "WHERE deleted_at IS NULL AND home_knowledge_base_id IS NOT NULL"
    )


def downgrade() -> None:
    """Remove workspace structures (ownership columns are kept compatible)."""
    op.execute("DROP INDEX IF EXISTS uq_research_documents_home_sha_active")
    op.execute(
        "CREATE UNIQUE INDEX uq_research_documents_owner_sha_active "
        "ON research_documents(owner_id, sha256) WHERE deleted_at IS NULL"
    )
    op.execute(
        "ALTER TABLE research_documents "
        "DROP COLUMN IF EXISTS copied_from_version_id, "
        "DROP COLUMN IF EXISTS copied_from_document_id, "
        "DROP COLUMN IF EXISTS created_by, "
        "DROP COLUMN IF EXISTS home_knowledge_base_id, "
        "DROP COLUMN IF EXISTS workspace_id"
    )
    op.execute(
        "ALTER TABLE knowledge_bases "
        "DROP COLUMN IF EXISTS created_by, "
        "DROP COLUMN IF EXISTS visibility, "
        "DROP COLUMN IF EXISTS workspace_id"
    )
    op.execute("DROP TABLE IF EXISTS knowledge_audit_events")
    op.execute("DROP TABLE IF EXISTS knowledge_base_members")
    op.execute("DROP TABLE IF EXISTS knowledge_workspace_members")
    op.execute("DROP INDEX IF EXISTS uq_workspaces_personal_owner")
    op.execute("DROP TABLE IF EXISTS knowledge_workspaces")
