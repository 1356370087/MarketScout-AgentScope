"""持续情报的计划、授权、观察、研究工作项与投递账本。"""

from alembic import op

revision = "0023_continuous_intelligence"
down_revision = "0022_team_task_fields"
branch_labels = depends_on = None

AUDIT = """id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    create_time timestamptz NOT NULL DEFAULT now(), update_time timestamptz NOT NULL DEFAULT now(),
    created_at text, updated_at text, is_deleted boolean NOT NULL DEFAULT false"""

TABLES = {
    "intelligence_publications": """sequence bigserial NOT NULL UNIQUE, document_id uuid NOT NULL,
        generation_id uuid NOT NULL, previous_generation_id uuid, origin_item_id uuid""",
    "intelligence_plans": """workspace_id uuid NOT NULL REFERENCES knowledge_workspaces(id),
        knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id), owner_id uuid NOT NULL,
        name text NOT NULL, objective text NOT NULL, definition jsonb NOT NULL,
        status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','paused','archived')),
        mode text NOT NULL DEFAULT 'observe' CHECK(mode IN ('observe','auto')),
        version int NOT NULL DEFAULT 1, authorization_id uuid, last_dispatched_at timestamptz,
        baseline jsonb NOT NULL DEFAULT '{}', reason text""",
    "intelligence_authorizations": """plan_id uuid NOT NULL REFERENCES intelligence_plans(id),
        owner_id uuid NOT NULL, authz_version int NOT NULL, plan_version int NOT NULL,
        scope jsonb NOT NULL, expires_at timestamptz NOT NULL, revoked_at timestamptz,
        revoked_by uuid, reason text""",
    "intelligence_sources": """plan_id uuid NOT NULL REFERENCES intelligence_plans(id),
        definition jsonb NOT NULL, identity text NOT NULL, sequence bigint NOT NULL DEFAULT 0,
        snapshot_id uuid, checkpoint jsonb NOT NULL DEFAULT '{}', next_run_at timestamptz,
        last_checked_at timestamptz, failures int NOT NULL DEFAULT 0, error text,
        UNIQUE(plan_id,identity)""",
    "intelligence_snapshots": """plan_id uuid NOT NULL REFERENCES intelligence_plans(id),
        source_id uuid NOT NULL REFERENCES intelligence_sources(id), sequence bigint NOT NULL,
        raw_sha256 text NOT NULL, content_sha256 text NOT NULL, artifact text NOT NULL,
        metadata jsonb NOT NULL, observed_at timestamptz NOT NULL, document_id uuid, generation_id uuid,
        UNIQUE(source_id,sequence)""",
    "intelligence_events": """plan_id uuid NOT NULL REFERENCES intelligence_plans(id),
        source_id uuid NOT NULL REFERENCES intelligence_sources(id), sequence bigint NOT NULL,
        previous_snapshot_id uuid REFERENCES intelligence_snapshots(id), snapshot_id uuid REFERENCES intelligence_snapshots(id),
        kind text NOT NULL, status text NOT NULL DEFAULT 'pending', origin_item_id uuid,
        payload jsonb NOT NULL DEFAULT '{}',
        available_at timestamptz NOT NULL DEFAULT now(), UNIQUE(source_id,sequence)""",
    "intelligence_decisions": """plan_id uuid NOT NULL REFERENCES intelligence_plans(id),
        plan_version int NOT NULL, authorization_id uuid NOT NULL REFERENCES intelligence_authorizations(id),
        event_ids jsonb NOT NULL, assessment jsonb NOT NULL, would_start boolean NOT NULL DEFAULT false,
        job_id uuid NOT NULL UNIQUE""",
    "intelligence_items": """plan_id uuid NOT NULL REFERENCES intelligence_plans(id),
        authorization_id uuid NOT NULL REFERENCES intelligence_authorizations(id), owner_id uuid NOT NULL,
        decision_id uuid UNIQUE REFERENCES intelligence_decisions(id), question text NOT NULL,
        status text NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','queued','running','awaiting_review','completed','blocked','cancelled','dismissed')),
        version int NOT NULL DEFAULT 1, manual boolean NOT NULL DEFAULT false,
        run_id text, run_sequence int NOT NULL DEFAULT 1, run_request jsonb, manifest jsonb NOT NULL DEFAULT '{}',
        quality_status text NOT NULL DEFAULT 'unassessed', result jsonb NOT NULL DEFAULT '{}',
        review jsonb NOT NULL DEFAULT '{}', reason text""",
    "intelligence_item_events": """item_id uuid NOT NULL REFERENCES intelligence_items(id),
        event_id uuid NOT NULL UNIQUE REFERENCES intelligence_events(id), UNIQUE(item_id,event_id)""",
    "intelligence_item_runs": """item_id uuid NOT NULL REFERENCES intelligence_items(id),
        run_id text NOT NULL UNIQUE, execution_number int NOT NULL DEFAULT 1, owner_id uuid,
        purpose text NOT NULL DEFAULT 'research', result jsonb NOT NULL DEFAULT '{}',
        UNIQUE(item_id,run_id), UNIQUE(item_id,execution_number)""",
    "intelligence_jobs": """plan_id uuid REFERENCES intelligence_plans(id), owner_id uuid,
        kind text NOT NULL, business_key text NOT NULL UNIQUE, payload jsonb NOT NULL DEFAULT '{}',
        result jsonb NOT NULL DEFAULT '{}', status text NOT NULL DEFAULT 'queued',
        attempts int NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
        lease_until timestamptz, started_at timestamptz, completed_at timestamptz, error text""",
    "intelligence_budget_periods": """plan_id uuid NOT NULL REFERENCES intelligence_plans(id),
        period text NOT NULL, reserved bigint NOT NULL DEFAULT 0 CHECK(reserved>=0),
        settled bigint NOT NULL DEFAULT 0 CHECK(settled>=0), UNIQUE(plan_id,period)""",
    "intelligence_budget_reservations": """plan_id uuid NOT NULL REFERENCES intelligence_plans(id),
        operation_key text NOT NULL UNIQUE, period text NOT NULL, amount bigint NOT NULL CHECK(amount>0),
        actual bigint, status text NOT NULL DEFAULT 'reserved' CHECK(status IN ('reserved','unknown','settled','released'))""",
    "intelligence_notifications": """plan_id uuid NOT NULL REFERENCES intelligence_plans(id),
        item_id uuid REFERENCES intelligence_items(id), recipient_id uuid NOT NULL,
        business_key text NOT NULL UNIQUE, kind text NOT NULL, payload jsonb NOT NULL DEFAULT '{}',
        read_at timestamptz, delivery_status text NOT NULL DEFAULT 'pending', message_id text""",
    "intelligence_feishu_bindings": """user_id uuid NOT NULL UNIQUE, app_id text NOT NULL, tenant_key text NOT NULL,
        open_id text NOT NULL, enabled boolean NOT NULL DEFAULT false,
        UNIQUE(app_id,tenant_key,open_id)""",
    "intelligence_oauth_states": """user_id uuid NOT NULL, app_id text NOT NULL, state_hash text NOT NULL UNIQUE,
        expires_at timestamptz NOT NULL, consumed_at timestamptz""",
    "intelligence_commands": """actor_id uuid NOT NULL, command_key text NOT NULL,
        request_hash text NOT NULL, response jsonb NOT NULL, UNIQUE(actor_id,command_key)""",
}


def upgrade():
    for table, columns in TABLES.items():
        op.execute(f"CREATE TABLE {table} ({AUDIT}, {columns})")
        op.execute(f"COMMENT ON TABLE {table} IS '持续情报业务记录；按计划与知识库隔离'")
    op.execute("CREATE INDEX ix_intelligence_sources_due ON intelligence_sources(next_run_at) WHERE NOT is_deleted")
    op.execute("CREATE INDEX ix_intelligence_publications_document ON intelligence_publications(document_id,sequence)")
    op.execute("CREATE INDEX ix_intelligence_jobs_ready ON intelligence_jobs(status,available_at)")
    op.execute("CREATE INDEX ix_intelligence_events_pending ON intelligence_events(plan_id,status,available_at)")
    op.execute("CREATE INDEX ix_intelligence_items_active ON intelligence_items(plan_id,status)")
    op.execute("CREATE INDEX ix_intelligence_inbox ON intelligence_notifications(recipient_id,create_time DESC)")
    op.execute("ALTER TABLE research_document_jobs ADD COLUMN intelligence_binding jsonb")


def downgrade():
    op.execute("ALTER TABLE research_document_jobs DROP COLUMN intelligence_binding")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
