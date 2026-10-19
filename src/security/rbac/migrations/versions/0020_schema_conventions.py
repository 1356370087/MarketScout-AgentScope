"""Normalize business tables to the shared identity and audit contract.

The application uses ``run_id`` as a domain scope.  It is not the database
identity of a row, so this migration gives every business table an ``id``
primary key while preserving the previous primary key columns as unique
business keys.  The five audit fields are deliberately kept alongside the
existing ``created_at``/``updated_at`` fields for compatibility with older
queries and exports.
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op


revision = "0020_schema_conventions"
down_revision = "0019_platform_management"
branch_labels = depends_on = None

_EXCLUDED_TABLES = {"alembic_version"}
_AUDIT_COLUMNS = ("create_time", "update_time", "created_at", "updated_at", "is_deleted")

_COLUMN_COMMENTS = {
    "id": "记录唯一标识",
    "run_id": "研究运行标识，用于业务范围隔离",
    "task_id": "任务业务标识",
    "member_id": "团队成员标识",
    "owner": "当前负责成员标识",
    "owner_id": "所属用户标识",
    "created_at": "创建人用户标识",
    "updated_at": "更新人用户标识",
    "create_time": "记录创建时间",
    "update_time": "记录更新时间",
    "is_deleted": "软删除标记，false 表示有效",
    "status": "当前状态",
    "version": "乐观并发版本号",
    "sequence": "按创建顺序递增的序号",
    "event_id": "事件唯一标识",
    "operation_id": "幂等操作标识",
    "message_key": "消息去重键",
    "snapshot": "任务状态快照",
    "metadata": "扩展元数据",
}


def _ident(value: str) -> str:
    """Quote a PostgreSQL identifier obtained from the catalog."""
    return '"' + value.replace('"', '""') + '"'


def _literal(value: str) -> str:
    """Quote a SQL string literal without relying on a user-controlled value."""
    return "'" + value.replace("'", "''") + "'"


def _short_name(table: str, suffix: str) -> str:
    """Return a deterministic identifier within PostgreSQL's 63-byte limit."""
    raw = f"uq_{table}_{suffix}"
    if len(raw) <= 60:
        return raw
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return raw[:51] + "_" + digest


def _tables(conn) -> list[dict[str, object]]:
    rows = conn.execute(
        sa.text(
            """SELECT c.oid, c.relname
               FROM pg_class c
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relkind='r'
               ORDER BY c.relname"""
        )
    ).mappings()
    return [dict(row) for row in rows if row["relname"] not in _EXCLUDED_TABLES]


def _foreign_keys(conn) -> list[dict[str, str]]:
    rows = conn.execute(
        sa.text(
            """SELECT c.relname AS table_name, con.conname,
                      pg_get_constraintdef(con.oid, true) AS definition
               FROM pg_constraint con
               JOIN pg_class c ON c.oid=con.conrelid
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE con.contype='f' AND n.nspname='public'
               ORDER BY c.relname, con.conname"""
        )
    ).mappings()
    return [dict(row) for row in rows if row["table_name"] not in _EXCLUDED_TABLES]


def _primary_keys(conn) -> dict[str, dict[str, object]]:
    rows = conn.execute(
        sa.text(
            """SELECT c.relname AS table_name, con.conname,
                      array_agg(a.attname ORDER BY key.ord) AS columns
               FROM pg_constraint con
               JOIN pg_class c ON c.oid=con.conrelid
               JOIN pg_namespace n ON n.oid=c.relnamespace
               JOIN unnest(con.conkey) WITH ORDINALITY AS key(attnum, ord) ON true
               JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=key.attnum
               WHERE con.contype='p' AND n.nspname='public'
               GROUP BY c.relname, con.conname
               ORDER BY c.relname"""
        )
    ).mappings()
    return {
        row["table_name"]: {"name": row["conname"], "columns": list(row["columns"])}
        for row in rows
        if row["table_name"] not in _EXCLUDED_TABLES
    }


def _unique_column_sets(conn, table: str) -> set[tuple[str, ...]]:
    rows = conn.execute(
        sa.text(
            """SELECT array_agg(a.attname ORDER BY key.ord) AS columns
               FROM pg_constraint con
               JOIN pg_class c ON c.oid=con.conrelid
               JOIN pg_namespace n ON n.oid=c.relnamespace
               JOIN unnest(con.conkey) WITH ORDINALITY AS key(attnum, ord) ON true
               JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=key.attnum
               WHERE n.nspname='public' AND c.relname=:table_name
                 AND con.contype='u'
               GROUP BY con.oid"""
        ),
        {"table_name": table},
    ).mappings()
    return {tuple(row["columns"]) for row in rows}


def _columns(conn, table: str) -> dict[str, dict[str, object]]:
    rows = conn.execute(
        sa.text(
            """SELECT column_name, data_type, udt_name, is_nullable
               FROM information_schema.columns
               WHERE table_schema='public' AND table_name=:table_name"""
        ),
        {"table_name": table},
    ).mappings()
    return {row["column_name"]: dict(row) for row in rows}


def _add_id(conn, table: str, cols: dict[str, dict[str, object]]) -> None:
    table_sql = _ident(table)
    if "id" not in cols:
        conn.execute(sa.text(f"ALTER TABLE {table_sql} ADD COLUMN id uuid"))
        conn.execute(sa.text(f"UPDATE {table_sql} SET id=gen_random_uuid() WHERE id IS NULL"))
        conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN id SET DEFAULT gen_random_uuid()"))
        conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN id SET NOT NULL"))
        return

    id_info = cols["id"]
    if id_info["is_nullable"] == "YES":
        expression = "gen_random_uuid()" if id_info["udt_name"] == "uuid" else "gen_random_uuid()::text"
        conn.execute(sa.text(f"UPDATE {table_sql} SET id={expression} WHERE id IS NULL"))
        conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN id SET NOT NULL"))
    if id_info["udt_name"] == "uuid":
        conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN id SET DEFAULT gen_random_uuid()"))
    elif id_info["udt_name"] in {"varchar", "text", "bpchar"}:
        conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN id SET DEFAULT gen_random_uuid()::text"))


def _ensure_audit_columns(conn, table: str, cols: dict[str, dict[str, object]]) -> None:
    table_sql = _ident(table)
    # Add the new canonical fields first.  They are nullable during backfill so
    # existing records keep their historical timestamps where available.
    for name in _AUDIT_COLUMNS:
        if name not in cols:
            sql_type = "boolean" if name == "is_deleted" else "timestamptz"
            conn.execute(sa.text(f"ALTER TABLE {table_sql} ADD COLUMN {_ident(name)} {sql_type}"))
            cols[name] = {"data_type": "boolean" if name == "is_deleted" else "timestamp with time zone", "udt_name": "bool" if name == "is_deleted" else "timestamptz", "is_nullable": "YES"}

    # Fill canonical timestamps.  Existing varchar timestamps (platform
    # metadata) retain their string representation; new fields are timestamptz.
    for name, source in (
        ("create_time", "clock_timestamp()"),
        ("update_time", "clock_timestamp()"),
        ("created_at", "clock_timestamp()"),
        ("updated_at", "clock_timestamp()"),
    ):
        info = cols[name]
        if info["data_type"] in {"timestamp with time zone", "timestamp without time zone", "date"}:
            if name == "create_time" and "created_at" in cols:
                source = "COALESCE(created_at, clock_timestamp())" if cols["created_at"]["data_type"] in {"timestamp with time zone", "timestamp without time zone", "date"} else "clock_timestamp()"
            elif name == "update_time" and "updated_at" in cols:
                source = "COALESCE(updated_at, clock_timestamp())" if cols["updated_at"]["data_type"] in {"timestamp with time zone", "timestamp without time zone", "date"} else "clock_timestamp()"
            elif name == "created_at" and "create_time" in cols:
                source = "COALESCE(create_time, clock_timestamp())"
            elif name == "updated_at" and "update_time" in cols:
                source = "COALESCE(update_time, clock_timestamp())"
            conn.execute(sa.text(f"UPDATE {table_sql} SET {_ident(name)}={source} WHERE {_ident(name)} IS NULL"))
            conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN {_ident(name)} SET DEFAULT now()"))
        else:
            conn.execute(sa.text(f"UPDATE {table_sql} SET {_ident(name)}=COALESCE(NULLIF({_ident(name)}, ''), to_char(clock_timestamp(), 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')) WHERE {_ident(name)} IS NULL OR {_ident(name)}=''"))
            conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN {_ident(name)} SET DEFAULT to_char(clock_timestamp(), 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')"))
        conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN {_ident(name)} SET NOT NULL"))

    conn.execute(sa.text(f"UPDATE {table_sql} SET is_deleted=false WHERE is_deleted IS NULL"))
    conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN is_deleted SET DEFAULT false"))
    conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN is_deleted SET NOT NULL"))


def _ensure_update_trigger(conn, table: str) -> None:
    table_sql = _ident(table)
    trigger_name = _short_name(table, "audit_update")
    conn.execute(sa.text(f"DROP TRIGGER IF EXISTS {_ident(trigger_name)} ON {table_sql}"))
    conn.execute(
        sa.text(
            f"CREATE TRIGGER {_ident(trigger_name)} BEFORE UPDATE ON {table_sql} "
            "FOR EACH ROW EXECUTE FUNCTION set_schema_audit_update_time()"
        )
    )


def _comment_schema(conn, table: str) -> None:
    table_sql = _ident(table)
    conn.execute(sa.text(f"COMMENT ON TABLE {table_sql} IS {_literal('业务表：' + table)}"))
    for column in _columns(conn, table):
        comment = _COLUMN_COMMENTS.get(column, f"字段：{column}")
        conn.execute(sa.text(f"COMMENT ON COLUMN {table_sql}.{_ident(column)} IS {_literal(comment)}"))


def upgrade() -> None:
    """Apply the shared primary-key, audit-field and comment contract."""
    conn = op.get_bind()
    tables = _tables(conn)
    primary_keys = _primary_keys(conn)
    foreign_keys = _foreign_keys(conn)

    # Drop all public foreign keys while primary keys are being replaced.  The
    # definitions are restored after legacy keys receive unique constraints.
    for foreign_key in foreign_keys:
        conn.execute(
            sa.text(
                f"ALTER TABLE {_ident(foreign_key['table_name'])} "
                f"DROP CONSTRAINT {_ident(foreign_key['conname'])}"
            )
        )

    for table_info in tables:
        table = str(table_info["relname"])
        primary = primary_keys.get(table)
        if primary and primary["columns"] != ["id"]:
            old_columns = list(primary["columns"])
            unique_sets = _unique_column_sets(conn, table)
            if tuple(old_columns) not in unique_sets:
                constraint = _short_name(table, "legacy_key")
                columns_sql = ", ".join(_ident(column) for column in old_columns)
                conn.execute(
                    sa.text(
                        f"ALTER TABLE {_ident(table)} ADD CONSTRAINT {_ident(constraint)} "
                        f"UNIQUE ({columns_sql})"
                    )
                )

        cols = _columns(conn, table)
        _add_id(conn, table, cols)
        _ensure_audit_columns(conn, table, cols)

        if not primary or primary["columns"] != ["id"]:
            if primary:
                conn.execute(
                    sa.text(
                        f"ALTER TABLE {_ident(table)} DROP CONSTRAINT {_ident(str(primary['name']))}"
                    )
                )
            conn.execute(sa.text(f"ALTER TABLE {_ident(table)} ADD PRIMARY KEY (id)"))

        _comment_schema(conn, table)

    conn.execute(
        sa.text(
            """CREATE OR REPLACE FUNCTION set_schema_audit_update_time()
               RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN
                   NEW.update_time = clock_timestamp();
                   RETURN NEW;
               END;
               $$"""
        )
    )
    # The function must exist before triggers are created on a fresh database.
    # Recreate the triggers after installing it.
    for table_info in tables:
        _ensure_update_trigger(conn, str(table_info["relname"]))

    for foreign_key in foreign_keys:
        conn.execute(
            sa.text(
                f"ALTER TABLE {_ident(foreign_key['table_name'])} "
                f"ADD CONSTRAINT {_ident(foreign_key['conname'])} {foreign_key['definition']}"
            )
        )


def downgrade() -> None:
    """Keep the normalized contract when moving Alembic to an older revision.

    Removing the canonical ``id`` keys would discard the identity contract and
    could invalidate rows created after this migration.  Earlier migrations
    still drop their own tables safely, so the downgrade intentionally leaves
    these additive columns in place while allowing Alembic to continue.
    """
