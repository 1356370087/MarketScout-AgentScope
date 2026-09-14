"""Knowledge health coverage targets (KB-16)."""

from alembic import op

revision = "0015_knowledge_health"
down_revision = "0014_knowledge_lifecycle"
branch_labels = depends_on = None


def upgrade():
    """Persist business coverage requirements per competitor and period."""
    op.execute("""CREATE TABLE knowledge_health_targets (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
        company varchar(200) NOT NULL, period varchar(100) NOT NULL,
        topic varchar(200) NOT NULL, min_documents integer NOT NULL DEFAULT 1 CHECK(min_documents BETWEEN 1 AND 100),
        max_age_days integer NOT NULL DEFAULT 0 CHECK(max_age_days BETWEEN 0 AND 3650),
        created_by uuid NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(knowledge_base_id,company,period,topic))""")


def downgrade():
    """Remove the coverage-target table."""
    op.drop_table("knowledge_health_targets")
