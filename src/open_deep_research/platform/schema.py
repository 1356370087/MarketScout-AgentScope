"""控制面关系模型；通过显式 Alembic 迁移安装。"""

from sqlalchemy import (MetaData, Table, Column, String, Integer, BigInteger,
                        Boolean, JSON, Text, LargeBinary, UniqueConstraint)

metadata = MetaData()

state = Table("platform_state", metadata,
    Column("id", Integer, primary_key=True),
    Column("initialized", Boolean, nullable=False, default=False),
    Column("active_revision", String(36)),
    Column("generation", Integer, nullable=False, default=0))

profiles = Table("platform_profiles", metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(160), nullable=False),
    Column("owner_id", String(36)),
    Column("scope", String(16), nullable=False),
    Column("created_at", String(40), nullable=False))

revisions = Table("platform_revisions", metadata,
    Column("id", String(36), primary_key=True),
    Column("profile_id", String(36), nullable=False, index=True),
    Column("number", Integer, nullable=False),
    Column("version", Integer, nullable=False, default=1),
    Column("status", String(24), nullable=False),
    Column("document", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("base_revision", String(36)),
    Column("created_by", String(36), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("published_at", String(40)),
    UniqueConstraint("profile_id", "number"))

applications = Table("platform_applications", metadata,
    Column("revision_id", String(36), primary_key=True),
    Column("component", String(48), primary_key=True),
    Column("status", String(24), nullable=False),
    Column("detail", JSON, nullable=False),
    Column("updated_at", String(40), nullable=False))

credentials = Table("platform_credentials", metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(160), nullable=False),
    Column("owner_id", String(36)),
    Column("provider", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("disabled", Boolean, nullable=False, default=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("key_id", String(32), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False))

preferences = Table("platform_preferences", metadata,
    Column("owner_id", String(36), primary_key=True),
    Column("profile_revision_id", String(36)),
    Column("configurable", JSON, nullable=False),
    Column("publication_theme", JSON, nullable=False))

credential_versions = Table("platform_credential_versions", metadata,
    Column("credential_id", String(36), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("key_id", String(32), nullable=False),
    Column("created_at", String(40), nullable=False))

quotas = Table("platform_quotas", metadata,
    Column("owner_id", String(36), primary_key=True),
    Column("limits", JSON, nullable=False),
    Column("version", Integer, nullable=False, default=1))

months = Table("platform_quota_months", metadata,
    Column("owner_id", String(36), primary_key=True),
    Column("period", String(7), primary_key=True),
    Column("reserved", BigInteger, nullable=False, default=0),
    Column("settled", BigInteger, nullable=False, default=0))

charges = Table("platform_quota_operations", metadata,
    Column("operation_id", String(320), primary_key=True),
    Column("owner_id", String(36), nullable=False, index=True),
    Column("period", String(7), nullable=False),
    Column("status", String(24), nullable=False),
    Column("reserved", BigInteger, nullable=False),
    Column("actual", BigInteger),
    Column("source", String(48), nullable=False),
    Column("detail", JSON, nullable=False, default=dict),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False))

audit = Table("platform_audit_events", metadata,
    Column("id", String(36), primary_key=True),
    Column("actor_id", String(36), nullable=False, index=True),
    Column("action", String(96), nullable=False),
    Column("subject_id", String(320)),
    Column("detail", JSON, nullable=False),
    Column("created_at", String(40), nullable=False, index=True))

job_bindings = Table("platform_job_bindings", metadata,
    Column("job_id", String(96), primary_key=True),
    Column("owner_id", String(36), nullable=False),
    Column("revision_id", String(36), nullable=False),
    Column("created_at", String(40), nullable=False))
