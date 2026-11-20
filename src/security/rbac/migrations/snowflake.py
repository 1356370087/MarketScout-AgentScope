"""PostgreSQL identity generator shared by the business and native runtime schemas."""

import sqlalchemy as sa


def ensure_snowflake_generator(conn):
    """Install once per database, including databases used by standalone native workers."""
    exists = sa.text("SELECT pg_catalog.to_regprocedure('public.marketscout_snowflake_id()') IS NOT NULL")
    if conn.scalar(exists):
        return
    # Different migrations can bootstrap the two schemas concurrently.
    conn.exec_driver_sql("SELECT pg_catalog.pg_advisory_xact_lock(1297306449, 1)")
    if conn.scalar(exists):
        return
    conn.exec_driver_sql("""CREATE SEQUENCE public.marketscout_snowflake_ticks AS bigint
        MINVALUE 0 MAXVALUE 9007199254740991 START 0 CACHE 1 NO CYCLE""")
    conn.exec_driver_sql("""
        CREATE FUNCTION public.marketscout_snowflake_id() RETURNS bigint
        LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            wall_ms bigint;
            tick bigint;
        BEGIN
            -- One database issues IDs for node zero. Release the serialization lock
            -- after reserving one tick, not at the end of the caller's transaction.
            BEGIN
                PERFORM pg_advisory_lock(1297306449, 0);
                wall_ms := floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint - 1704067200000;
                IF wall_ms >= (1::bigint << 41) THEN
                    RAISE EXCEPTION 'MarketScout snowflake timestamp exhausted';
                END IF;
                tick := nextval('public.marketscout_snowflake_ticks'::regclass);
                IF tick < (wall_ms << 12) THEN
                    tick := setval('public.marketscout_snowflake_ticks'::regclass, wall_ms << 12, true);
                END IF;
                PERFORM pg_advisory_unlock(1297306449, 0);
            EXCEPTION WHEN query_canceled OR assert_failure OR OTHERS THEN
                PERFORM pg_advisory_unlock(1297306449, 0);
                RAISE;
            END;
            RETURN ((tick >> 12) << 22) | (tick & 4095);
        END $$
    """)
    conn.exec_driver_sql("""COMMENT ON FUNCTION public.marketscout_snowflake_id() IS
        '单库雪花 ID：自 2024-01-01 UTC 起的 41 位逻辑毫秒、10 位节点（本库为 0）、12 位毫秒内序号。回拨和序号溢出继续递增。'""")
    conn.exec_driver_sql("GRANT EXECUTE ON FUNCTION public.marketscout_snowflake_id() TO PUBLIC")
