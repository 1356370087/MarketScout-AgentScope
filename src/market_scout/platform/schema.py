"""控制面关系模型；通过显式 Alembic 迁移安装。"""

from sqlalchemy import (MetaData, Table, Column, String, Integer, BigInteger,
                        Boolean, JSON, Text, LargeBinary, DateTime,
                        UniqueConstraint, text)

metadata = MetaData()


def _audit_columns() -> list[Column]:
    """Return the shared row-audit fields used by every platform table."""
    return [
        Column("create_time", DateTime(timezone=True), nullable=False, server_default=text("now()")),
        Column("update_time", DateTime(timezone=True), nullable=False, server_default=text("now()")),
        Column("created_at", String(128), nullable=True),
        Column("updated_at", String(128), nullable=True),
        Column("is_deleted", Boolean, nullable=False, server_default=text("false")),
    ]

state = Table("platform_state", metadata,
    Column("id", Integer, primary_key=True),
    Column("initialized", Boolean, nullable=False, default=False),
    Column("active_revision", String(36)),
    Column("generation", Integer, nullable=False, default=0),
    *_audit_columns())

profiles = Table("platform_profiles", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("name", String(160), nullable=False),
    Column("owner_id", String(36)),
    Column("scope", String(16), nullable=False),
    *_audit_columns())

revisions = Table("platform_revisions", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("profile_id", String(36), nullable=False, index=True),
    Column("number", Integer, nullable=False),
    Column("version", Integer, nullable=False, default=1),
    Column("status", String(24), nullable=False),
    Column("document", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("base_revision", String(36)),
    Column("created_by", String(36), nullable=False),
    Column("published_at", String(40)),
    *_audit_columns(),
    UniqueConstraint("profile_id", "number"))

applications = Table("platform_applications", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("revision_id", String(36), nullable=False),
    Column("component", String(48), nullable=False),
    Column("status", String(24), nullable=False),
    Column("detail", JSON, nullable=False),
    *_audit_columns(),
    UniqueConstraint("revision_id", "component"))

credentials = Table("platform_credentials", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("name", String(160), nullable=False),
    Column("owner_id", String(36)),
    Column("provider", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("disabled", Boolean, nullable=False, default=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("key_id", String(32), nullable=False),
    *_audit_columns())

preferences = Table("platform_preferences", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("owner_id", String(36), nullable=False),
    Column("profile_revision_id", String(36)),
    Column("configurable", JSON, nullable=False),
    Column("publication_theme", JSON, nullable=False),
    *_audit_columns(),
    UniqueConstraint("owner_id"))

credential_versions = Table("platform_credential_versions", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("credential_id", String(36), nullable=False),
    Column("version", Integer, nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("key_id", String(32), nullable=False),
    *_audit_columns(),
    UniqueConstraint("credential_id", "version"))

quotas = Table("platform_quotas", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("owner_id", String(36), nullable=False),
    Column("limits", JSON, nullable=False),
    Column("version", Integer, nullable=False, default=1),
    *_audit_columns(),
    UniqueConstraint("owner_id"))

months = Table("platform_quota_months", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("owner_id", String(36), nullable=False),
    Column("period", String(7), nullable=False),
    Column("reserved", BigInteger, nullable=False, default=0),
    Column("settled", BigInteger, nullable=False, default=0),
    *_audit_columns(),
    UniqueConstraint("owner_id", "period"))

charges = Table("platform_quota_operations", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("operation_id", String(320), nullable=False),
    Column("owner_id", String(36), nullable=False, index=True),
    Column("period", String(7), nullable=False),
    Column("status", String(24), nullable=False),
    Column("reserved", BigInteger, nullable=False),
    Column("actual", BigInteger),
    Column("source", String(48), nullable=False),
    Column("detail", JSON, nullable=False, default=dict),
    *_audit_columns(),
    UniqueConstraint("operation_id"))

audit = Table("platform_audit_events", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("actor_id", String(36), nullable=False, index=True),
    Column("action", String(96), nullable=False),
    Column("subject_id", String(320)),
    Column("detail", JSON, nullable=False),
    *_audit_columns())

job_bindings = Table("platform_job_bindings", metadata,
    Column("id", String(36), primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("job_id", String(96), nullable=False),
    Column("owner_id", String(36), nullable=False),
    Column("revision_id", String(36), nullable=False),
    *_audit_columns(),
    UniqueConstraint("job_id"))
