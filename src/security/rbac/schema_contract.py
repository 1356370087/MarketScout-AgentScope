"""PostgreSQL schemas for application queries and database migrations."""

# Application identifiers remain valid across the physical row-key migration.
# Alembic and backup tools use the actual public tables, not the compatibility views.
BUSINESS_SEARCH_PATH = "marketscout_compat,public"
BUSINESS_SCHEMA_HEAD = "0025_business_time_seconds"
