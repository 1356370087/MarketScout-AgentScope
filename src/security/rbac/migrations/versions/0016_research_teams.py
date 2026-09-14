"""Persistent research teams and RocketMQ transaction outcomes."""

from alembic import op

revision = "0016_research_teams"
down_revision = "0015_knowledge_health"
branch_labels = depends_on = None


def upgrade():
    """Create coordination state independently of IAM authentication mode."""
    statements = """
        CREATE TABLE research_teams (
            run_id text PRIMARY KEY, name text NOT NULL,
            status text NOT NULL DEFAULT 'active', fence_token bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE research_team_members (
            run_id text NOT NULL REFERENCES research_teams(run_id),
            member_id text NOT NULL, name text NOT NULL, purpose text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'idle', current_task_id text,
            session jsonb NOT NULL DEFAULT '{}', version bigint NOT NULL DEFAULT 1,
            PRIMARY KEY(run_id, member_id), UNIQUE(run_id, name)
        );
        CREATE TABLE research_team_tasks (
            run_id text NOT NULL REFERENCES research_teams(run_id), task_id text NOT NULL,
            snapshot jsonb NOT NULL, owner text, status text NOT NULL DEFAULT 'pending',
            admission_status text NOT NULL DEFAULT 'pending', version bigint NOT NULL,
            fence_token bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(run_id, task_id)
        );
        CREATE UNIQUE INDEX research_team_member_active_task
            ON research_team_tasks(run_id, owner)
            WHERE owner IS NOT NULL AND status IN ('running', 'waiting_for_confirmation');
        CREATE TABLE research_team_dependencies (
            run_id text NOT NULL, task_id text NOT NULL, blocker_id text NOT NULL,
            PRIMARY KEY(run_id, task_id, blocker_id), CHECK(task_id <> blocker_id),
            FOREIGN KEY(run_id, task_id) REFERENCES research_team_tasks(run_id, task_id),
            FOREIGN KEY(run_id, blocker_id) REFERENCES research_team_tasks(run_id, task_id)
        );
        CREATE TABLE research_coordination_transactions (
            run_id text NOT NULL, operation_id text NOT NULL, event_id text NOT NULL UNIQUE,
            state text NOT NULL CHECK(state IN ('PREPARED', 'COMMITTED', 'ABORTED')),
            event jsonb NOT NULL, result jsonb,
            deadline timestamptz NOT NULL DEFAULT now() + interval '30 seconds',
            PRIMARY KEY(run_id, operation_id)
        );
        CREATE TABLE research_coordination_events (
            sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            event_id text NOT NULL UNIQUE, run_id text NOT NULL, event jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX research_coordination_event_run
            ON research_coordination_events(run_id, sequence);
        CREATE TABLE research_coordination_receipts (
            event_id text NOT NULL REFERENCES research_coordination_events(event_id),
            recipient text NOT NULL, applied boolean NOT NULL DEFAULT false,
            PRIMARY KEY(event_id, recipient)
        );
    """
    for statement in statements.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    """Remove only the coordination tables introduced by this revision."""
    for name in (
        "research_coordination_receipts", "research_coordination_events",
        "research_coordination_transactions", "research_team_dependencies",
        "research_team_tasks", "research_team_members", "research_teams",
    ):
        op.drop_table(name)
