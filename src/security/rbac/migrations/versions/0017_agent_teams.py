"""Member-owned teams, task plans and transactional message delivery."""

from alembic import op

revision = "0017_agent_teams"
down_revision = "0016_research_teams"
branch_labels = depends_on = None

DDL = """
ALTER TABLE research_teams ADD COLUMN mode text NOT NULL DEFAULT 'collaborator';
ALTER TABLE research_teams ADD COLUMN execution_mode text NOT NULL DEFAULT 'direct';
ALTER TABLE research_team_members ADD COLUMN execution_mode text NOT NULL DEFAULT 'direct';
ALTER TABLE research_team_members ADD COLUMN mode_override boolean NOT NULL DEFAULT false;
ALTER TABLE research_team_members ADD COLUMN execution_token text;
ALTER TABLE research_team_members ADD COLUMN execution_epoch bigint NOT NULL DEFAULT 0;
ALTER TABLE research_team_members ADD COLUMN lease_expires timestamptz;
ALTER TABLE research_team_tasks ADD COLUMN phase text;
ALTER TABLE research_team_tasks ADD COLUMN execution_mode text;
ALTER TABLE research_team_tasks ADD COLUMN plan_version integer NOT NULL DEFAULT 0;
ALTER TABLE research_team_tasks ADD COLUMN plan_revision_limit integer NOT NULL DEFAULT 4;
ALTER TABLE research_team_tasks ADD COLUMN metadata jsonb NOT NULL DEFAULT '{}';
CREATE TABLE research_team_plans (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id text NOT NULL, task_id text NOT NULL, version integer NOT NULL,
    owner text NOT NULL, execution_epoch bigint NOT NULL,
    request_id text NOT NULL UNIQUE, content jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending', feedback text NOT NULL DEFAULT '',
    reviewed_by text, create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
    created_at text, updated_at text,
    is_deleted boolean NOT NULL DEFAULT false, reviewed_at timestamptz,
    UNIQUE(run_id,task_id,version),
    FOREIGN KEY(run_id,task_id) REFERENCES research_team_tasks(run_id,task_id)
);
CREATE TABLE research_team_proposals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id text NOT NULL UNIQUE, run_id text NOT NULL REFERENCES research_teams(run_id),
    member_id text NOT NULL, content jsonb NOT NULL, status text NOT NULL DEFAULT 'pending',
    task_id text, create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
    created_at text, updated_at text,
    is_deleted boolean NOT NULL DEFAULT false
);
CREATE TABLE research_coordination_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id text NOT NULL UNIQUE REFERENCES research_coordination_events(event_id),
    attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz, delivery_error text,
    lease_token text, lease_expires timestamptz,
    create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
    created_at text, updated_at text,
    is_deleted boolean NOT NULL DEFAULT false
);
CREATE INDEX research_coordination_outbox_pending ON research_coordination_outbox(next_attempt)
    WHERE published_at IS NULL;
CREATE TABLE research_coordination_rejections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_key text NOT NULL UNIQUE, reason text NOT NULL,
    create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
    created_at text, updated_at text,
    is_deleted boolean NOT NULL DEFAULT false
);
"""


def upgrade():
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    for table in (
        "research_coordination_rejections",
        "research_coordination_outbox",
        "research_team_proposals",
        "research_team_plans",
    ):
        op.drop_table(table)
    for table, columns in {
        "research_team_tasks": (
            "phase",
            "execution_mode",
            "plan_version",
            "plan_revision_limit",
            "metadata",
        ),
        "research_team_members": (
            "execution_mode",
            "mode_override",
            "execution_token",
            "execution_epoch",
            "lease_expires",
        ),
        "research_teams": ("mode", "execution_mode"),
    }.items():
        for column in columns:
            op.drop_column(table, column)
