"""Only an explicitly supplied independent remote database may be mutated."""

import os
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def domain(monkeypatch, tmp_path):
    dsn = os.environ.get("INTELLIGENCE_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("INTELLIGENCE_TEST_DATABASE_URL not configured")
    target = urlsplit(dsn)
    if target.hostname != "172.22.208.30" or not target.path.startswith("/marketscout_intelligence_acceptance_"):
        pytest.fail("intelligence integration requires its dedicated remote acceptance database")
    monkeypatch.setenv("PLATFORM_CONFIGURATION_MODE", "explicit")
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", dsn.replace("postgresql+asyncpg://", "postgresql://"))
    monkeypatch.setenv("DOCUMENT_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("IAM_DATABASE_URL", dsn.replace("postgresql://", "postgresql+asyncpg://"))
    monkeypatch.setenv("INTELLIGENCE_ENABLED", "true")
    monkeypatch.setenv("RESPECT_ROBOTS_TXT", "false")
    from security.rbac.settings import get_settings
    from security.rbac.database import dispose_engine
    from open_deep_research.documents.database import get_document_pool, close_document_pool
    await close_document_pool()
    await dispose_engine()
    pool = await get_document_pool()
    try:
        actor, workspace, base = str(uuid4()), str(uuid4()), str(uuid4())
        async with pool.acquire() as c:
            await c.execute("INSERT INTO iam_users(id,email,email_normalized,status,authz_version) VALUES($1::uuid,$2,$2,'active',1)", actor, actor + "@example.invalid")
            await c.execute("INSERT INTO iam_user_roles(user_id,role_id) SELECT $1::uuid,id FROM iam_roles WHERE code='researcher'", actor)
            await c.execute("INSERT INTO knowledge_workspaces(id,kind,name,created_by) VALUES($1::uuid,'personal','Intelligence test',$2::uuid)", workspace, actor)
            await c.execute("INSERT INTO knowledge_workspace_members(workspace_id,user_id,role) VALUES($1::uuid,$2::uuid,'owner')", workspace, actor)
            await c.execute("INSERT INTO knowledge_bases(id,owner_id,name,workspace_id) VALUES($1::uuid,$2::uuid,'Acceptance',$3::uuid)", base, actor, workspace)
        from security.rbac.principal import Principal
        from security.rbac.permissions import RESEARCH_RUN_CREATE, DOCUMENT_READ_OWN
        principal = Principal(actor, actor + "@example.invalid", "active", None, frozenset({"researcher"}),
                              frozenset({RESEARCH_RUN_CREATE.code, DOCUMENT_READ_OWN.code}), 1)
        from open_deep_research.intelligence.store import Store
        from open_deep_research.intelligence.contracts import PlanCreate, SourceInput, PlanBudget, Activate
        from open_deep_research.intelligence import authorization
        async def no_platform(_principal, _definition):
            return None
        monkeypatch.setattr(authorization, "validate_runtime_configuration", no_platform)
        store = Store(pool)
        body = PlanCreate(workspace_id=workspace, knowledge_base_id=base, name="Pricing", objective="Track material price changes",
            sources=[SourceInput(url="https://example.com/pricing")], budget=PlanBudget(run_micro_usd=1_000_000, month_micro_usd=4_000_000))
        plan = await store.create(actor, body)
        plan = await store.activate(principal, plan["id"], Activate(expected_version=plan["version"], command_id=str(uuid4()), mode="observe", expires_at=datetime.now(UTC)+timedelta(days=1)))
        yield SimpleNamespace(pool=pool, store=store, actor=actor, workspace=workspace, base=base, principal=principal, plan=plan, tmp_path=tmp_path)
    finally:
        if 'plan' in locals():
            await pool.execute("UPDATE intelligence_plans SET status='archived' WHERE id=$1::uuid", plan["id"])
            await pool.execute("UPDATE intelligence_jobs SET status='completed' WHERE plan_id=$1::uuid AND status IN ('queued','running')", plan["id"])
            await pool.execute("UPDATE intelligence_items SET status='cancelled' WHERE plan_id=$1::uuid AND status IN ('queued','running')", plan["id"])
        await close_document_pool()
        await dispose_engine()
