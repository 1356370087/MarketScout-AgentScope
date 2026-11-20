"""Use physical snowflake primary keys while keeping the existing ID contract writable."""

import sqlalchemy as sa
from alembic import op

from security.rbac.migrations.snowflake import ensure_snowflake_generator


revision = "0024_snowflake_ids"
down_revision = "0023_continuous_intelligence"
branch_labels = depends_on = None

# Owned by revisions 0001–0023. A shared PostgreSQL installation may also host
# extension tables or third-party services in public; they are not migrations' targets.
TABLES = """
iam_audit_events iam_email_tokens iam_permissions iam_rate_limits iam_refresh_tokens
iam_role_permissions iam_roles iam_sessions iam_user_roles iam_users
intelligence_authorizations intelligence_budget_periods intelligence_budget_reservations
intelligence_commands intelligence_decisions intelligence_events intelligence_feishu_bindings
intelligence_item_events intelligence_item_runs intelligence_items intelligence_jobs
intelligence_notifications intelligence_oauth_states intelligence_plans intelligence_publications
intelligence_snapshots intelligence_sources
knowledge_audit_events knowledge_base_members knowledge_bases knowledge_batch_items knowledge_batches
knowledge_collections knowledge_content_fingerprints knowledge_document_links knowledge_editorial_audit
knowledge_entities knowledge_entity_aliases knowledge_eval_runs knowledge_eval_sets knowledge_fact_assertions
knowledge_fact_evidence knowledge_fact_keys knowledge_health_targets knowledge_jobs knowledge_model_attempts
knowledge_page_citations knowledge_page_revisions knowledge_pages knowledge_queries knowledge_saved_searches
knowledge_search_profiles knowledge_source_relations knowledge_sync_sources knowledge_usage_daily
knowledge_workspace_members knowledge_workspaces
platform_applications platform_audit_events platform_credential_versions platform_credentials
platform_job_bindings platform_preferences platform_profiles platform_quota_months platform_quota_operations
platform_quotas platform_revisions platform_state
research_coordination_events research_coordination_outbox research_coordination_receipts
research_coordination_rejections research_coordination_transactions research_document_chunks
research_document_generations research_document_jobs research_document_operations research_document_segments
research_document_units research_document_versions research_document_worker_heartbeats research_documents
research_generation_entity_links research_run_sources research_team_dependencies research_team_members
research_team_plans research_team_proposals research_team_tasks research_teams
""".split()


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _comment(conn, target, comment):
    if comment is not None:
        literal = conn.scalar(sa.text("SELECT quote_literal(:comment)"), {"comment": comment})
        conn.exec_driver_sql(f"COMMENT ON {target} IS {literal}")


def _catalog(conn, sql):
    return conn.execute(sa.text(sql), {"tables": TABLES}).mappings().all()


def _swap_keys(conn, *, upgrading):
    # All the FK definitions below must be deparsed against the physical schema,
    # regardless of the connection's regular compatibility search_path.
    conn.exec_driver_sql("SET LOCAL search_path = public")
    qualified = [f"public.{_quote(table)}" for table in TABLES]
    conn.exec_driver_sql("LOCK TABLE " + ", ".join(qualified) + " IN ACCESS EXCLUSIVE MODE")
    keys = _catalog(conn, """
        SELECT t.relname AS tbl, p.conname AS pk, p.condeferrable, p.condeferred,
               obj_description(p.oid, 'pg_constraint') AS comment,
               (SELECT c.conname FROM pg_constraint c WHERE c.conrelid=t.oid
                AND c.contype='u' AND c.conname='uq_' || t.relname || '_snowflake_id') AS parked
        FROM pg_class t JOIN pg_constraint p ON p.conrelid=t.oid AND p.contype='p'
        JOIN pg_attribute a ON a.attrelid=t.oid AND a.attname='id'
        WHERE t.relnamespace='public'::regnamespace AND t.relname=ANY(CAST(:tables AS name[]))
              AND p.conkey=ARRAY[a.attnum] ORDER BY t.relname
    """)
    if len(keys) != len(TABLES):
        raise RuntimeError("0024 requires the existing id primary key on every project business table")

    for row in keys:
        table = row["tbl"]
        relation = f"public.{_quote(table)}"
        if upgrading:
            conn.exec_driver_sql(f"ALTER TABLE {relation} RENAME id TO legacy_id")
            if row["parked"]:
                # The application may already have exported these IDs before a downgrade.
                conn.exec_driver_sql(f"ALTER TABLE {relation} RENAME snowflake_id TO id")
            else:
                # A volatile default backfills existing rows without firing update triggers.
                conn.exec_driver_sql(f"ALTER TABLE {relation} ADD COLUMN id bigint NOT NULL DEFAULT public.marketscout_snowflake_id()")
                conn.exec_driver_sql(f"COMMENT ON COLUMN {relation}.id IS '记录的雪花主键；legacy_id 保留已有业务标识及其关联'")
        else:
            conn.exec_driver_sql(f"ALTER TABLE {relation} RENAME id TO snowflake_id")
            conn.exec_driver_sql(f"ALTER TABLE {relation} RENAME legacy_id TO id")

    # Dropping an FK target index requires rebinding its FKs, even if an equivalent
    # unique index exists. Capture definitions AFTER renames so column dependencies,
    # ON DELETE/UPDATE, deferral, validation and comments retain their meanings.
    references = _catalog(conn, """
        SELECT ns.nspname AS schem, child.relname AS tbl, fk.conname AS name,
               pg_get_constraintdef(fk.oid) AS definition, obj_description(fk.oid, 'pg_constraint') AS comment
        FROM pg_constraint fk JOIN pg_constraint target ON target.conrelid=fk.confrelid AND target.conindid=fk.conindid
        JOIN pg_class parent ON parent.oid=target.conrelid JOIN pg_class child ON child.oid=fk.conrelid
        JOIN pg_namespace ns ON ns.oid=child.relnamespace
        WHERE fk.contype='f' AND parent.relnamespace='public'::regnamespace
          AND parent.relname=ANY(CAST(:tables AS name[])) AND (target.contype='p' OR
            (target.contype='u' AND target.conname IN ('uq_' || parent.relname || '_legacy_id', 'uq_' || parent.relname || '_snowflake_id')))
        ORDER BY ns.nspname, child.relname, fk.conname
    """)
    for fk in references:
        if fk["schem"] != "public" or fk["tbl"] not in TABLES:
            raise RuntimeError(f"0024 cannot rebind an externally owned FK: {fk['schem']}.{fk['tbl']}.{fk['name']}")
        conn.exec_driver_sql(f"ALTER TABLE public.{_quote(fk['tbl'])} DROP CONSTRAINT {_quote(fk['name'])}")

    for row in keys:
        table = row["tbl"]
        relation = f"public.{_quote(table)}"
        legacy_key = _quote(f"uq_{table}_legacy_id")
        snowflake_key = _quote(f"uq_{table}_snowflake_id")
        pk = _quote(row["pk"])
        deferral = " DEFERRABLE" if row["condeferrable"] else ""
        if row["condeferred"]:
            deferral += " INITIALLY DEFERRED"
        changes = [f"DROP CONSTRAINT {pk}"]
        if upgrading:
            if row["parked"]:
                changes.append(f"DROP CONSTRAINT {snowflake_key}")
            changes.append(f"ADD CONSTRAINT {legacy_key} UNIQUE (legacy_id){deferral}")
        else:
            changes.extend([f"DROP CONSTRAINT {legacy_key}", f"ADD CONSTRAINT {snowflake_key} UNIQUE (snowflake_id)"])
        changes.append(f"ADD CONSTRAINT {pk} PRIMARY KEY (id){deferral}")
        conn.exec_driver_sql(f"ALTER TABLE {relation} " + ", ".join(changes))
        _comment(conn, f"CONSTRAINT {pk} ON {relation}", row["comment"])

    for fk in references:
        table = f"public.{_quote(fk['tbl'])}"
        name = _quote(fk["name"])
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD CONSTRAINT {name} {fk['definition']}")
        _comment(conn, f"CONSTRAINT {name} ON {table}", fk["comment"])


def upgrade():
    conn = op.get_bind()
    ensure_snowflake_generator(conn)
    _swap_keys(conn, upgrading=True)
    conn.exec_driver_sql("CREATE SCHEMA marketscout_compat")
    conn.exec_driver_sql("GRANT USAGE ON SCHEMA marketscout_compat TO PUBLIC")
    columns = _catalog(conn, """
        SELECT t.relname AS tbl, array_agg(a.attname ORDER BY a.attnum) AS names
        FROM pg_class t JOIN pg_attribute a ON a.attrelid=t.oid
        WHERE t.relnamespace='public'::regnamespace AND t.relname=ANY(CAST(:tables AS name[]))
          AND a.attnum>0 AND NOT a.attisdropped AND a.attname<>'id' GROUP BY t.relname
    """)
    for row in columns:
        name = _quote(row["tbl"])
        fields = ", ".join("legacy_id AS id" if c == "legacy_id" else _quote(c) for c in row["names"])
        conn.exec_driver_sql(f"CREATE VIEW marketscout_compat.{name} WITH (security_invoker=true) AS SELECT {fields} FROM public.{name}")
        # The invoker still needs the original base table/column grants and passes its RLS policies.
        conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE ON marketscout_compat.{name} TO PUBLIC")


def downgrade():
    conn = op.get_bind()
    for table in TABLES:
        conn.exec_driver_sql(f"DROP VIEW marketscout_compat.{_quote(table)}")
    conn.exec_driver_sql("DROP SCHEMA marketscout_compat")
    # Keep already-issued snowflake values and their generator for a later re-upgrade.
    _swap_keys(conn, upgrading=False)
