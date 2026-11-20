"""Upgrade project-owned PostgreSQL keys without changing AgentScope SDK tables."""

from sqlalchemy import text

from security.rbac.migrations.snowflake import ensure_snowflake_generator


def prepare_runtime_row_ids(connection, table_names, create_business_tables=None):
    """Install physical IDs while preserving the keys used by the runtime."""
    ensure_snowflake_generator(connection)
    # Concurrent auto-create or migration callers must observe the same old PK
    # before renaming it. Hold the schema lock through the caller's creation DDL.
    connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':marketscout-runtime-tables', 0))")
    if create_business_tables is not None:
        create_business_tables(connection)
    tables = connection.execute(text("""
        SELECT n.nspname AS schema, t.relname AS name, k.conname AS pk,
               ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY u(n,ord)
                     JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=u.n ORDER BY u.ord) AS columns,
               pg_get_expr(d.adbin,d.adrelid) AS id_default
        FROM pg_class t JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_constraint k ON k.conrelid=t.oid AND k.contype='p'
        LEFT JOIN pg_attribute id ON id.attrelid=t.oid AND id.attname='id' AND NOT id.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid=t.oid AND d.adnum=id.attnum
        WHERE n.nspname=current_schema() AND t.relname=ANY(CAST(:names AS text[]))
        ORDER BY t.relname
        """), {"names": list(table_names)}).mappings().all()
    quote = connection.dialect.identifier_preparer.quote_identifier
    for table in tables:
        target = f"{quote(table['schema'])}.{quote(table['name'])}"
        old_key = table['columns']
        if old_key == ['id']:
            default = table['id_default'] or ''
            if default in {'marketscout_snowflake_id()', 'public.marketscout_snowflake_id()'}:
                continue
            if not default.startswith('nextval('):
                raise ValueError(f"unexpected legacy ID default on {target}")
            # Existing queue IDs, returned cursors, order and the owned serial
            # sequence must survive for consumers that resume after deployment.
            connection.exec_driver_sql(f'ALTER TABLE {target} RENAME COLUMN id TO position')
            old_key = ['position']
        keys = ', '.join(quote(name) for name in old_key)
        connection.exec_driver_sql(f'''ALTER TABLE {target}
            DROP CONSTRAINT {quote(table['pk'])}, ADD UNIQUE ({keys}),
            ADD COLUMN id bigint DEFAULT public.marketscout_snowflake_id() PRIMARY KEY''')
