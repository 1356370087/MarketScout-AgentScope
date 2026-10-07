"""Versioned knowledge indexes and per-attempt service accounting."""

from alembic import op

revision = "0018_knowledge_execution"
down_revision = "0017_agent_teams"
branch_labels = depends_on = None


def upgrade():
    """Extend existing generations; create atomic service-call budgets."""
    op.execute("ALTER TABLE research_document_generations ADD COLUMN index_profile jsonb NOT NULL DEFAULT '{}'")
    op.execute("""UPDATE research_document_generations g SET index_profile=jsonb_build_object(
        'model', x.model, 'dimensions', x.dimensions, 'revision', 'v1')
        FROM (SELECT generation_id, min(embedding_model) AS model,
                     max(vector_dims(embedding)) AS dimensions
              FROM research_document_segments WHERE embedding IS NOT NULL
              GROUP BY generation_id HAVING count(DISTINCT embedding_model)=1
                   AND count(DISTINCT vector_dims(embedding))=1) x
        WHERE g.id=x.generation_id""")
    op.execute("""CREATE TABLE knowledge_usage_daily (
        owner_id uuid NOT NULL, day date NOT NULL, attempts bigint NOT NULL DEFAULT 0,
        PRIMARY KEY(owner_id,day))""")
    op.execute("""CREATE TABLE knowledge_model_attempts (
        id uuid PRIMARY KEY, query_id uuid NOT NULL, owner_id uuid NOT NULL,
        operation text NOT NULL, model text NOT NULL, status text NOT NULL,
        usage jsonb, error_code text, started_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz)
    """)
    op.execute("CREATE INDEX ix_knowledge_attempt_query ON knowledge_model_attempts(query_id)")


def downgrade():
    """Remove only the added accounting and index-profile objects."""
    op.drop_table("knowledge_model_attempts")
    op.drop_table("knowledge_usage_daily")
    op.drop_column("research_document_generations", "index_profile")
