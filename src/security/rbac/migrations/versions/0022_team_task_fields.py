"""Give team task titles and descriptions an authoritative SQL representation."""

from alembic import op

revision = "0022_team_task_fields"
down_revision = "0021_audit_actor_columns"
branch_labels = depends_on = None

DDL = """
ALTER TABLE research_team_tasks ADD COLUMN subject text;
ALTER TABLE research_team_tasks ADD COLUMN description text;
UPDATE research_team_tasks SET
    subject = coalesce(nullif(snapshot->>'display_title', ''),
                       nullif(snapshot->>'subject', ''),
                       nullif(snapshot->>'research_topic', ''), task_id),
    description = coalesce(snapshot->>'research_topic', snapshot->>'description', '');
ALTER TABLE research_team_tasks ALTER COLUMN subject SET NOT NULL;
ALTER TABLE research_team_tasks ALTER COLUMN description SET NOT NULL;
COMMENT ON COLUMN research_team_tasks.subject IS '任务标题，使用简短明确的行动描述';
COMMENT ON COLUMN research_team_tasks.description IS '任务详细说明，包含研究目标、范围和交付要求';
"""


def upgrade():
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    # Preserve edits for readers of the previous snapshot-based schema.
    op.execute("""UPDATE research_team_tasks SET snapshot=snapshot ||
                  jsonb_build_object('display_title',subject,'research_topic',description)""")
    op.drop_column("research_team_tasks", "description")
    op.drop_column("research_team_tasks", "subject")
