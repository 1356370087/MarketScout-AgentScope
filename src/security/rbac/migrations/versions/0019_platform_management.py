"""平台配置版本、加密凭据、月度配额与运营权限。"""

from alembic import op
import sqlalchemy as sa

revision = "0019_platform_management"
down_revision = "0018_knowledge_execution"
branch_labels = depends_on = None


def upgrade():
    from open_deep_research.platform.schema import metadata, state
    from security.rbac.platform_permissions import CATALOG, ADMIN_GRANTS, DEVELOPER_GRANTS
    bind = op.get_bind()
    metadata.create_all(bind)
    bind.execute(state.insert().values(id=1, initialized=False, generation=0))
    for code, name in CATALOG.items():
        bind.execute(sa.text("""INSERT INTO iam_permissions(code,name,description,domain,created_at)
            VALUES (:code,:name,:description,'platform',now()) ON CONFLICT(code) DO NOTHING"""),
            {"code": code, "name": name, "description": name})
    for role, codes in (("admin", ADMIN_GRANTS), ("developer", DEVELOPER_GRANTS)):
        for code in codes:
            bind.execute(sa.text("""INSERT INTO iam_role_permissions(role_id,permission_id)
                SELECT r.id,p.id FROM iam_roles r CROSS JOIN iam_permissions p
                WHERE r.code=:role AND p.code=:code ON CONFLICT DO NOTHING"""), {"role": role, "code": code})
    bind.execute(sa.text("""UPDATE platform_state SET initialized=true WHERE EXISTS (
        SELECT 1 FROM iam_user_roles ur JOIN iam_roles r ON r.id=ur.role_id WHERE r.code='admin')"""))


def downgrade():
    from open_deep_research.platform.schema import metadata
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM iam_role_permissions WHERE permission_id IN (SELECT id FROM iam_permissions WHERE domain='platform')"))
    bind.execute(sa.text("DELETE FROM iam_permissions WHERE domain='platform'"))
    metadata.drop_all(bind)
