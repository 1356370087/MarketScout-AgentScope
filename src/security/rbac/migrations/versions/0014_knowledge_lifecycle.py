"""Durable accumulation, separate wiki publication pointer and export jobs."""

from alembic import op

revision = "0014_knowledge_lifecycle"
down_revision = "0013_wiki"
branch_labels = depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE knowledge_pages ADD COLUMN published_revision_id uuid REFERENCES knowledge_page_revisions(id)"
    )
    op.execute(
        "UPDATE knowledge_pages p SET published_revision_id=(SELECT id FROM knowledge_page_revisions r WHERE r.page_id=p.id AND status='published' ORDER BY revision_number DESC LIMIT 1)"
    )
    op.execute(
        "ALTER TABLE knowledge_fact_assertions ADD COLUMN adopted boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_fact_candidate ON knowledge_fact_assertions(knowledge_base_id, extraction_key) WHERE extraction_key IS NOT NULL AND extraction_key<>''"
    )
    op.execute("""CREATE TABLE knowledge_jobs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
        actor_id uuid NOT NULL, kind text NOT NULL, business_key text NOT NULL UNIQUE,
        payload jsonb NOT NULL DEFAULT '{}', result jsonb NOT NULL DEFAULT '{}',
        status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','failed')),
        attempts int NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
        lease_until timestamptz, error text, created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""")
    op.execute(
        "CREATE INDEX ix_knowledge_jobs_ready ON knowledge_jobs(status,available_at)"
    )
    op.execute("""CREATE TABLE knowledge_editorial_audit (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), knowledge_base_id uuid NOT NULL,
        actor_id uuid NOT NULL, action text NOT NULL, target_id uuid NOT NULL,
        detail jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now())""")


def downgrade():
    op.execute("DROP TABLE knowledge_editorial_audit")
    op.execute("DROP TABLE knowledge_jobs")
    op.execute("DROP INDEX uq_fact_candidate")
    op.execute("ALTER TABLE knowledge_fact_assertions DROP COLUMN adopted")
    op.execute("ALTER TABLE knowledge_pages DROP COLUMN published_revision_id")
