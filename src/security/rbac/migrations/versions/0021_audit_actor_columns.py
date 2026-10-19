"""Use audit actor identifiers in ``created_at`` and ``updated_at``.

``create_time`` and ``update_time`` are the temporal audit fields.  The two
``*_at`` fields are retained for the project's established column names but
now store the user or service identifier that performed the operation.
Historical timestamps are already preserved in the canonical time fields by
revision 0020; when an actor cannot be reconstructed the actor fields remain
NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0021_audit_actor_columns"
down_revision = "0020_schema_conventions"
branch_labels = depends_on = None

_EXCLUDED_TABLES = {"alembic_version"}
_ACTOR_COLUMNS = ("created_at", "updated_at")


def _ident(value: str) -> str:
    """Quote a catalog identifier."""
    return '"' + value.replace('"', '""') + '"'


def _literal(value: str) -> str:
    """Quote a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _tables(conn) -> list[str]:
    rows = conn.execute(
        sa.text(
            """SELECT c.relname
               FROM pg_class c
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relkind='r'
               ORDER BY c.relname"""
        )
    ).scalars()
    return [str(name) for name in rows if name not in _EXCLUDED_TABLES]


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


def _ensure_time_column(conn, table: str, column: str) -> None:
    table_sql = _ident(table)
    cols = _columns(conn, table)
    if column not in cols:
        conn.execute(
            sa.text(
                f"ALTER TABLE {table_sql} ADD COLUMN {_ident(column)} "
                "timestamptz NOT NULL DEFAULT now()"
            )
        )
    else:
        info = cols[column]
        if info["is_nullable"] == "YES":
            conn.execute(
                sa.text(
                    f"UPDATE {table_sql} SET {_ident(column)}=clock_timestamp() "
                    f"WHERE {_ident(column)} IS NULL"
                )
            )
            conn.execute(
                sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN {_ident(column)} SET NOT NULL")
            )
        if info["data_type"] in {
            "timestamp with time zone",
            "timestamp without time zone",
            "date",
        }:
            conn.execute(
                sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN {_ident(column)} SET DEFAULT now()")
            )


def _convert_actor_column(conn, table: str, column: str) -> None:
    table_sql = _ident(table)
    cols = _columns(conn, table)
    if column not in cols:
        conn.execute(sa.text(f"ALTER TABLE {table_sql} ADD COLUMN {_ident(column)} text"))
    else:
        info = cols[column]
        conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN {_ident(column)} DROP DEFAULT"))
        conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN {_ident(column)} DROP NOT NULL"))
        if info["data_type"] not in {"text", "character varying", "character"}:
            # Revision 0020 copied historical timestamps into create_time and
            # update_time.  Do not expose those timestamps as actor IDs.
            conn.execute(
                sa.text(
                    f"ALTER TABLE {table_sql} ALTER COLUMN {_ident(column)} "
                    "TYPE text USING NULL::text"
                )
            )
        else:
            # Platform metadata was temporarily created with ISO timestamp
            # strings.  Keep real actor identifiers and clear only those
            # generated timestamp values.
            conn.execute(
                sa.text(
                    f"UPDATE {table_sql} SET {_ident(column)}=NULL "
                    f"WHERE {_ident(column)} ~ {_literal(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T')}"
                )
            )
    conn.execute(sa.text(f"ALTER TABLE {table_sql} ALTER COLUMN {_ident(column)} DROP NOT NULL"))


def _comment_actor_columns(conn, table: str) -> None:
    table_sql = _ident(table)
    conn.execute(sa.text(f"COMMENT ON COLUMN {table_sql}.created_at IS {_literal('创建人用户标识')}"))
    conn.execute(sa.text(f"COMMENT ON COLUMN {table_sql}.updated_at IS {_literal('更新人用户标识')}"))


def upgrade() -> None:
    """Convert the two actor columns while preserving historical times."""
    conn = op.get_bind()
    for table in _tables(conn):
        _ensure_time_column(conn, table, "create_time")
        _ensure_time_column(conn, table, "update_time")
        for column in _ACTOR_COLUMNS:
            _convert_actor_column(conn, table, column)
        _comment_actor_columns(conn, table)


def downgrade() -> None:
    """Keep actor identifiers when moving the migration marker backwards."""
