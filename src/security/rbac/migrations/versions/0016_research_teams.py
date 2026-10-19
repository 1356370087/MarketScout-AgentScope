"""Persistent research teams and RocketMQ transaction outcomes."""

from alembic import op

revision = "0016_research_teams"
down_revision = "0015_knowledge_health"
branch_labels = depends_on = None


def upgrade():
    """Create coordination state independently of IAM authentication mode."""
    statements = """
        CREATE EXTENSION IF NOT EXISTS pgcrypto;
        CREATE TABLE research_teams (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id text NOT NULL UNIQUE, name text NOT NULL,
            status text NOT NULL DEFAULT 'active', fence_token bigint NOT NULL,
            create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
            created_at text, updated_at text,
            is_deleted boolean NOT NULL DEFAULT false
        );
        CREATE TABLE research_team_members (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id text NOT NULL REFERENCES research_teams(run_id),
            member_id text NOT NULL, name text NOT NULL, purpose text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'idle', current_task_id text,
            session jsonb NOT NULL DEFAULT '{}', version bigint NOT NULL DEFAULT 1,
            create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
            created_at text, updated_at text,
            is_deleted boolean NOT NULL DEFAULT false,
            UNIQUE(run_id, member_id), UNIQUE(run_id, name)
        );
        CREATE TABLE research_team_tasks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id text NOT NULL REFERENCES research_teams(run_id), task_id text NOT NULL,
            snapshot jsonb NOT NULL, owner text, status text NOT NULL DEFAULT 'pending',
            admission_status text NOT NULL DEFAULT 'pending', version bigint NOT NULL,
            fence_token bigint NOT NULL, create_time timestamptz NOT NULL DEFAULT now(),
            update_time timestamptz NOT NULL DEFAULT now(), created_at text,
            updated_at text, is_deleted boolean NOT NULL DEFAULT false,
            UNIQUE(run_id, task_id)
        );
        CREATE UNIQUE INDEX research_team_member_active_task
            ON research_team_tasks(run_id, owner)
            WHERE owner IS NOT NULL AND status IN ('running', 'waiting_for_confirmation');
        CREATE TABLE research_team_dependencies (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id text NOT NULL, task_id text NOT NULL, blocker_id text NOT NULL,
            create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
            created_at text, updated_at text,
            is_deleted boolean NOT NULL DEFAULT false,
            UNIQUE(run_id, task_id, blocker_id), CHECK(task_id <> blocker_id),
            FOREIGN KEY(run_id, task_id) REFERENCES research_team_tasks(run_id, task_id),
            FOREIGN KEY(run_id, blocker_id) REFERENCES research_team_tasks(run_id, task_id)
        );
        CREATE TABLE research_coordination_transactions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id text NOT NULL, operation_id text NOT NULL, event_id text NOT NULL UNIQUE,
            state text NOT NULL CHECK(state IN ('PREPARED', 'COMMITTED', 'ABORTED')),
            event jsonb NOT NULL, result jsonb,
            deadline timestamptz NOT NULL DEFAULT now() + interval '30 seconds',
            create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
            created_at text, updated_at text,
            is_deleted boolean NOT NULL DEFAULT false,
            UNIQUE(run_id, operation_id)
        );
        CREATE TABLE research_coordination_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            event_id text NOT NULL UNIQUE, run_id text NOT NULL, event jsonb NOT NULL,
            create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
            created_at text, updated_at text,
            is_deleted boolean NOT NULL DEFAULT false
        );
        CREATE INDEX research_coordination_event_run
            ON research_coordination_events(run_id, sequence);
        CREATE TABLE research_coordination_receipts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            event_id text NOT NULL REFERENCES research_coordination_events(event_id),
            recipient text NOT NULL, applied boolean NOT NULL DEFAULT false,
            create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
            created_at text, updated_at text,
            is_deleted boolean NOT NULL DEFAULT false,
            UNIQUE(event_id, recipient)
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
