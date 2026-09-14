"""Add personal local-document research and pgvector indexes.

Revision ID: 0003_local_documents
Revises: 0002_sandbox_permissions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from security.rbac.permissions import PERMISSIONS
from security.rbac.roles import SYSTEM_ROLES

revision: str = "0003_local_documents"
down_revision: str | None = "0002_sandbox_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODES = {"document.read_own", "document.write_own", "research.tool.document"}


def upgrade() -> None:
    """Create document metadata, chunks, jobs, Run bindings and grants."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """CREATE TABLE research_documents (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL,
          filename varchar(240) NOT NULL, media_type varchar(160) NOT NULL,
          size_bytes bigint NOT NULL CHECK(size_bytes > 0), sha256 char(64) NOT NULL,
          storage_key text NOT NULL, status varchar(24) NOT NULL,
          failure_code varchar(96), page_count integer, chunk_count integer NOT NULL DEFAULT 0,
          ocr_pages integer NOT NULL DEFAULT 0, deleted_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT research_documents_status_check CHECK(status IN ('queued','processing','ready','failed','deleting'))
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_research_documents_owner_sha_active ON research_documents(owner_id,sha256) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_research_documents_owner_updated ON research_documents(owner_id,updated_at DESC)"
    )
    op.execute(
        """CREATE TABLE research_document_chunks (
          id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
          ordinal integer NOT NULL, locator varchar(320) NOT NULL, heading varchar(500), text text NOT NULL,
          search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple',coalesce(heading,'') || ' ' || text)) STORED,
          embedding vector(1536), embedding_model varchar(160), content_hash char(64) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(document_id,ordinal)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_document ON research_document_chunks(document_id)"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_fts ON research_document_chunks USING gin(search_vector)"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_text_trgm ON research_document_chunks USING gin(text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_heading_trgm ON research_document_chunks USING gin(heading gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_hnsw ON research_document_chunks USING hnsw(embedding vector_cosine_ops)"
    )
    op.execute(
        """CREATE TABLE research_document_jobs (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
          kind varchar(24) NOT NULL, status varchar(24) NOT NULL, attempts integer NOT NULL DEFAULT 0,
          available_at timestamptz NOT NULL DEFAULT now(), lease_expires_at timestamptz,
          worker_id varchar(200), error_code varchar(96), created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT research_document_jobs_kind_check CHECK(kind IN ('ingest','delete','reindex')),
          CONSTRAINT research_document_jobs_status_check CHECK(status IN ('queued','running','completed','failed'))
        )"""
    )
    op.execute(
        "CREATE INDEX ix_document_jobs_claim ON research_document_jobs(status,available_at,created_at)"
    )
    op.execute(
        """CREATE TABLE research_document_worker_heartbeats (
          worker_id varchar(200) PRIMARY KEY, heartbeat_at timestamptz NOT NULL
        )"""
    )
    op.execute(
        """CREATE TABLE research_run_sources (
          run_id varchar(64) NOT NULL, owner_id uuid NOT NULL,
          document_id uuid NOT NULL REFERENCES research_documents(id) ON DELETE RESTRICT,
          filename_snapshot varchar(240) NOT NULL, sha256_snapshot char(64) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(run_id,document_id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_research_run_sources_owner ON research_run_sources(owner_id,run_id)"
    )

    bind = op.get_bind()
    entries = [permission for permission in PERMISSIONS if permission.code in _CODES]
    bind.execute(
        sa.text(
            """INSERT INTO iam_permissions(code,name,description,domain)
               VALUES (:code,:name,:description,:domain)
               ON CONFLICT (code) DO NOTHING"""
        ),
        [
            {
                "code": item.code,
                "name": item.name,
                "description": item.description,
                "domain": item.domain,
            }
            for item in entries
        ],
    )
    permission_ids = dict(
        bind.execute(
            sa.text("SELECT code,id FROM iam_permissions WHERE code=ANY(:codes)"),
            {"codes": list(_CODES)},
        ).fetchall()
    )
    role_ids = dict(bind.execute(sa.text("SELECT code,id FROM iam_roles")).fetchall())
    grants = [
        {"role_id": role_ids[role.code], "permission_id": permission_ids[code]}
        for role in SYSTEM_ROLES
        for code in role.permissions
        if code in _CODES
    ]
    if grants:
        bind.execute(
            sa.text(
                """INSERT INTO iam_role_permissions(role_id,permission_id)
                   VALUES (:role_id,:permission_id)
                   ON CONFLICT DO NOTHING"""
            ),
            grants,
        )


def downgrade() -> None:
    """Remove local-document tables, grants and permission entries."""
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM iam_role_permissions WHERE permission_id IN (SELECT id FROM iam_permissions WHERE code=ANY(:codes))"
        ),
        {"codes": list(_CODES)},
    )
    bind.execute(
        sa.text("DELETE FROM iam_permissions WHERE code=ANY(:codes)"),
        {"codes": list(_CODES)},
    )
    op.drop_table("research_run_sources")
    op.drop_table("research_document_worker_heartbeats")
    op.drop_table("research_document_jobs")
    op.drop_table("research_document_chunks")
    op.drop_table("research_documents")
