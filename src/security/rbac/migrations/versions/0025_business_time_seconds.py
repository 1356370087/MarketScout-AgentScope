"""用北京时间展示业务时刻，保留底表精度、版本比较与租约语义。"""

from alembic import op
import sqlalchemy as sa


revision = "0025_business_time_seconds"
down_revision = "0024_snowflake_ids"
branch_labels = depends_on = None


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def _business_tables(conn):
    # 0024 只为项目拥有的 public 表创建同名兼容视图。无关表和 SDK 的表不进入展示层。
    return conn.exec_driver_sql("""
        SELECT t.relname FROM pg_class t
        JOIN pg_class v ON v.relname=t.relname AND v.relkind='v'
        WHERE t.relnamespace='public'::regnamespace AND t.relkind='r'
          AND v.relnamespace='marketscout_compat'::regnamespace ORDER BY t.relname
    """).scalars().all()


def _convert_platform_times(conn, table, columns, *, to_text=False):
    """Rebind the 0024 view in the same transaction, preserving its grants and owner."""
    ident = _quote(table)
    view = 'marketscout_compat.' + ident
    base = 'public.' + ident
    record = conn.execute(sa.text("""
        SELECT pg_get_viewdef(v.oid) AS definition, pg_get_userbyid(v.relowner) AS owner
        FROM pg_class v WHERE v.oid=to_regclass(:view)
    """), {"view": view}).mappings().one()
    grants = conn.execute(sa.text("""
        SELECT CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END AS role,
               a.privilege_type, a.is_grantable, acl.col
        FROM pg_class c CROSS JOIN LATERAL (
             SELECT NULL::text AS col,c.relacl AS permissions
             UNION ALL SELECT attname,attacl FROM pg_attribute WHERE attrelid=c.oid AND attacl IS NOT NULL
        ) acl CROSS JOIN LATERAL aclexplode(acl.permissions) a
        WHERE c.oid=to_regclass(:view) AND a.grantee<>c.relowner
    """), {"view": view}).mappings().all()
    # DROP RESTRICT allows an undeclared external dependency to stop the transaction intact.
    conn.exec_driver_sql(f"DROP VIEW {view} RESTRICT")
    for name in columns:
        field = _quote(name)
        conn.exec_driver_sql(f"ALTER TABLE {base} ALTER COLUMN {field} DROP DEFAULT")
        if to_text:
            expression = f"to_char({field} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
            target = 'varchar(40)'
        else:
            # Offset-bearing historical ISO strings retain their instant. The new unqualified
            # human format is Shanghai time; legacy unqualified ISO-T strings were UTC.
            source = f"NULLIF(btrim({field}),'')"
            expression = f"""CASE WHEN {source} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}} [0-9:]{{8}}(\\.[0-9]+)?$'
                THEN ({source})::timestamp AT TIME ZONE 'Asia/Shanghai' ELSE ({source})::timestamptz END"""
            target = 'timestamptz'
        conn.exec_driver_sql(f"ALTER TABLE {base} ALTER COLUMN {field} TYPE {target} USING ({expression})")
        if name in ('create_time', 'update_time'):
            conn.exec_driver_sql(f"ALTER TABLE {base} ALTER COLUMN {field} SET DEFAULT now()")
    conn.exec_driver_sql(f"CREATE VIEW {view} WITH (security_invoker=true) AS {record['definition']}")
    # A rebuilt view receives current default privileges. Restore its previous ACL explicitly.
    new_roles = conn.execute(sa.text("""SELECT DISTINCT CASE WHEN a.grantee=0 THEN 'PUBLIC'
        ELSE quote_ident(pg_get_userbyid(a.grantee)) END FROM pg_class c, LATERAL aclexplode(c.relacl) a
        WHERE c.oid=to_regclass(:view) AND a.grantee<>c.relowner"""), {"view": view}).scalars()
    for role in new_roles:
        conn.exec_driver_sql(f"REVOKE ALL PRIVILEGES ON {view} FROM {role}")
    for grant in grants:
        column = f" ({_quote(grant['col'])})" if grant['col'] else ''
        option = ' WITH GRANT OPTION' if grant['is_grantable'] else ''
        conn.exec_driver_sql(f"GRANT {grant['privilege_type']}{column} ON {view} TO {grant['role']}{option}")
    conn.exec_driver_sql(f"ALTER VIEW {view} OWNER TO {_quote(record['owner'])}")


def upgrade():
    conn = op.get_bind()
    previous_zone = conn.exec_driver_sql('SHOW TIME ZONE').scalar_one()
    conn.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
    tables = _business_tables(conn)
    for table in tables:
        if not table.startswith('platform_'):
            continue
        columns = conn.execute(sa.text("""SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:table AND data_type IN ('text','character varying')
              AND (column_name IN ('create_time','update_time') OR
                   (table_name='platform_revisions' AND column_name='published_at'))
            ORDER BY ordinal_position"""), {'table': table}).scalars().all()
        if columns:
            _convert_platform_times(conn, table, columns)
    conn.execute(sa.text("SELECT set_config('TimeZone', :tz, true)"), {'tz': previous_zone})
    conn.exec_driver_sql("""CREATE FUNCTION public.marketscout_business_time(timestamptz) RETURNS text
        LANGUAGE sql STABLE STRICT PARALLEL SAFE AS $$
        SELECT pg_catalog.to_char($1 AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') $$""")
    conn.exec_driver_sql('GRANT EXECUTE ON FUNCTION public.marketscout_business_time(timestamptz) TO PUBLIC')
    conn.exec_driver_sql('CREATE SCHEMA marketscout_display')
    conn.exec_driver_sql('GRANT USAGE ON SCHEMA marketscout_display TO PUBLIC')
    for table in tables:
        ident = _quote(table)
        columns = conn.execute(sa.text("""SELECT column_name,data_type FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:table ORDER BY ordinal_position"""), {'table': table})
        fields = []
        for name, data_type in columns:
            field = _quote(name)
            if data_type == 'timestamp with time zone':
                field = f'public.marketscout_business_time({field}) AS {field}'
            fields.append(field)
        # OFFSET 0 makes the whole view read-only, including columns without a time conversion.
        conn.exec_driver_sql(f"CREATE VIEW marketscout_display.{ident} WITH (security_invoker=true) AS "
                             f"SELECT {', '.join(fields)} FROM public.{ident} OFFSET 0")
        conn.exec_driver_sql(f"GRANT SELECT ON marketscout_display.{ident} TO PUBLIC")


def downgrade():
    conn = op.get_bind()
    for table in _business_tables(conn):
        conn.exec_driver_sql(f'DROP VIEW marketscout_display.{_quote(table)} RESTRICT')
    conn.exec_driver_sql('DROP SCHEMA marketscout_display RESTRICT')
    conn.exec_driver_sql('DROP FUNCTION public.marketscout_business_time(timestamptz) RESTRICT')
    # The pre-0025 publication column accepted ISO strings. Preserve UTC microseconds exactly.
    _convert_platform_times(conn, 'platform_revisions', ['published_at'], to_text=True)
