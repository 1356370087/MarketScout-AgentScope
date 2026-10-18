"""真实 PostgreSQL + IAM + 控制面 HTTP 集成；仅连接显式测试数据库。"""

import asyncio
import base64
import os
import json
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text, select

from open_deep_research.platform import runtime, schema as s, quota
from open_deep_research.platform.router import router, setup_router
from open_deep_research.platform.store import get_store

TEST_URL = os.getenv("PLATFORM_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="需要 PLATFORM_TEST_DATABASE_URL 指向专用测试库")


@pytest_asyncio.fixture
async def client(monkeypatch, tmp_path):
    assert TEST_URL and "/platform_test" in TEST_URL
    from security.rbac import database, jwt_service
    from security.rbac.app_extension import mount_rbac
    monkeypatch.setenv("IAM_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LOCAL_DEV_AUTH_BYPASS", "false")
    monkeypatch.setenv("PLATFORM_CONFIGURATION_MODE", "database")
    monkeypatch.setenv("PLATFORM_SETUP_TOKEN", "test-initialization-token")
    monkeypatch.setenv("PLATFORM_SECRET_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"t" * 32).decode())
    monkeypatch.setenv("PLATFORM_CONFIG_DIR", str(tmp_path / "config"))
    runtime.reset()
    database.reset_engine_for_tests()
    jwt_service.reset_cache_for_tests()
    engine = database.get_engine()
    async with engine.begin() as conn:
        # The test database is disposable and created specifically for this suite.
        await conn.run_sync(s.metadata.create_all)
        for table in reversed(s.metadata.sorted_tables):
            await conn.execute(table.delete())
        await conn.execute(s.state.insert().values(id=1, initialized=False, generation=0))
        await conn.execute(text("TRUNCATE iam_users, iam_rate_limits CASCADE"))
    app = FastAPI()
    mount_rbac(app)
    app.include_router(router)
    app.include_router(setup_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    runtime.reset()
    await database.dispose_engine()
    jwt_service.reset_cache_for_tests()


async def login(client):
    payload = {"token": "test-initialization-token", "email": "platform-admin@example.com", "password": "Test-Strong-Passphrase-42!"}
    response = await client.post("/setup/bootstrap", json=payload)
    assert response.status_code == 201, response.text
    response = await client.post("/auth/login", json={"email": payload["email"], "password": payload["password"]})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    return (await client.get("/auth/me")).json()


async def publish(client):
    response = await client.post("/platform/configurations", json={"name": "集成测试", "scope": "global", "document": {
        "services": {"PUBLISHER_ENABLED": False},
        "runtime": {"research_model": "openai:test", "max_researcher_iterations": 4,
                    "model_costs_per_million": {"openai:test": {"input": 1, "output": 2}}},
        "quotas": {"monthly_cost_micro_usd": 100, "max_run_cost_micro_usd": 50},
        "policy": {"max_researcher_iterations": {"editable": True, "minimum": 1, "maximum": 8}}}})
    assert response.status_code == 201, response.text
    draft = response.json()
    response = await client.post(f"/platform/configurations/{draft['id']}/publish", json={"expected_version": 1, "reason": "集成测试"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active", response.text
    return response.json()


@pytest.mark.asyncio
async def test_bootstrap_concurrency_and_persisted_iam_roles(client):
    payload = {"token": "test-initialization-token", "email": "platform-admin@example.com", "password": "Test-Strong-Passphrase-42!"}
    responses = await asyncio.gather(*(client.post("/setup/bootstrap", json=payload) for _ in range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert (await client.get("/setup/status")).json()["initialized"] is True
    response = await client.post("/auth/login", json={"email": payload["email"], "password": payload["password"]})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    identity = (await client.get("/auth/me")).json()
    assert {"admin", "researcher"} <= set(identity["roles"])
    assert "platform.config.publish" in identity["permissions"]


@pytest.mark.asyncio
async def test_publication_preview_preferences_and_environment_isolation(client, monkeypatch):
    await login(client)
    revision = await publish(client)
    monkeypatch.setenv("RESEARCH_MODEL", "openai:must-not-override")
    response = await client.post("/platform/configuration-preview", json={"configurable": {"max_researcher_iterations": 6}})
    assert response.status_code == 200, response.text
    assert response.json()["configurable"]["research_model"] == "openai:test"
    assert response.json()["platform_revision_id"] == revision["id"]
    forbidden = await client.post("/platform/configuration-preview", json={"configurable": {"max_researcher_iterations": 99}})
    assert forbidden.status_code == 422
    stale = await client.post("/platform/configuration-preview", json={"expected_platform_revision": str(uuid4())})
    assert stale.status_code == 409
    saved = await client.put("/platform/preferences", json={"profile_revision_id": revision["id"], "configurable": {"max_researcher_iterations": 5}})
    assert saved.status_code == 200, saved.text
    assert (await client.get("/platform/preferences")).json()["configurable"]["max_researcher_iterations"] == 5


@pytest.mark.asyncio
async def test_postgres_atomic_quota_and_encrypted_credentials(client):
    user = await login(client)
    await publish(client)
    response = await client.post("/platform/credentials", json={"name": "test-key", "scope": "global", "provider": "openai", "secret": "not-a-real-provider-key"})
    assert response.status_code == 201, response.text
    credential = response.json()
    assert "secret" not in credential and "ciphertext" not in credential
    assert "not-a-real-provider-key" not in (await client.get("/platform/credentials")).text
    outcomes = await asyncio.gather(*(quota.reserve(user["id"], f"pg-{i}", 60, "test") for i in range(5)), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    assert sum(isinstance(value, quota.QuotaExceeded) for value in outcomes) == 4
    events = (await client.get("/platform/audit")).json()["items"]
    assert any(item["action"] == "credential.created" for item in events)


@pytest.mark.asyncio
async def test_admin_can_make_one_user_monthly_unlimited_without_erasing_spend(client):
    user = await login(client)
    await publish(client)
    await quota.reserve(user["id"], "before-unlimited", 80, "test")
    await quota.mark_unknown("before-unlimited")
    response = await client.put(f"/platform/quotas/{user['id']}", json={"monthly_cost_unlimited": True})
    assert response.status_code == 200, response.text
    assert response.json()["effective"]["monthly_cost_unlimited"] is True
    assert response.json()["effective"]["max_run_cost_micro_usd"] == 50
    await asyncio.gather(*(quota.reserve(user["id"], f"no-ceiling-{i}", 600, "test") for i in range(5)))
    await quota.settle("no-ceiling-0", 400)
    await quota.settle("no-ceiling-0", 400)
    async with get_store().engine.connect() as conn:
        month = (await conn.execute(select(s.months).where(s.months.c.owner_id == user["id"]))).mappings().one()
        assert (month["reserved"], month["settled"]) == (2480, 400)
    researcher = await user_token(client, "quota-reader@example.com", "researcher")
    denied = await client.put(f"/platform/quotas/{user['id']}", headers={"Authorization": researcher},
        json={"monthly_cost_unlimited": True})
    assert denied.status_code == 403
    response = await client.put(f"/platform/quotas/{user['id']}", json={"monthly_cost_unlimited": False})
    assert response.status_code == 200
    with pytest.raises(quota.QuotaExceeded):
        await quota.reserve(user["id"], "limited-again", 1, "test")
    events = (await client.get("/platform/audit")).json()["items"]
    assert any(item["action"] == "quota.updated" and item["detail"]["limits"]["monthly_cost_unlimited"] for item in events)


@pytest.mark.asyncio
async def test_json_import_preserves_field_locks_and_budget(client):
    await login(client)
    source = {"runtime": {"max_researcher_iterations": 4}, "services": {"PUBLISHER_ENABLED": False},
              "policy": {"max_researcher_iterations": {"editable": True, "minimum": 1, "maximum": 8}},
              "quotas": {"monthly_cost_micro_usd": 5_000_000, "max_run_cost_micro_usd": 1_000_000}}
    response = await client.post("/platform/import", json={"name": "导出包回导", "scope": "global", "format": "json", "content": json.dumps({"document": source})})
    assert response.status_code == 201, response.text
    result = response.json()["document"]
    assert set(result["policy"]) == {"max_researcher_iterations"}
    assert result["policy"]["max_researcher_iterations"]["maximum"] == 8
    assert result["quotas"]["max_run_cost_micro_usd"] == 1_000_000


async def user_token(client, email, role_code):
    from security.rbac.database import session_scope
    from security.rbac.models import User, UserRole, Role, utcnow
    from security.rbac.passwords import hash_password
    async with session_scope() as session:
        user = User(email=email, email_normalized=email, password_hash=hash_password("Test-Strong-Passphrase-42!"), status="active", email_verified_at=utcnow())
        session.add(user)
        await session.flush()
        role = (await session.execute(select(Role).where(Role.code == role_code))).scalar_one()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        await session.commit()
    response = await client.post("/auth/login", json={"email": email, "password": "Test-Strong-Passphrase-42!"})
    assert response.status_code == 200, response.text
    return "Bearer " + response.json()["access_token"]


@pytest.mark.asyncio
async def test_private_models_cannot_be_selected_or_published_by_another_user(client, monkeypatch):
    from open_deep_research.platform.components import model_aliases
    from open_deep_research.agentscope_runtime.models import ROLES
    from open_deep_research.platform import components
    async def applied(_store, row):
        async with _store.engine.begin() as conn:
            await conn.execute(s.applications.update().where(s.applications.c.revision_id == row["id"]).values(status="applied"))
        return [{"status": "applied"}]
    monkeypatch.setattr(components, "apply_revision", applied)
    await login(client)
    admin_token = client.headers["Authorization"]
    def model(name, credential):
        return {"model_name": name, "litellm_params": {"model": "openai/test-only", "api_base": "http://127.0.0.1:9/v1", "api_key": "credential:" + credential},
                "model_info": {"max_input_tokens": 4096, "max_output_tokens": 128, "input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002}}
    shared = await client.post("/platform/credentials", json={"name": "shared", "scope": "global", "provider": "openai", "secret": "not-a-real-key"})
    document = {"runtime": {**{field: "shared" for field, _, _ in ROLES.values()}, "model_backend": "litellm"},
        "services": {"PUBLISHER_ENABLED": False}, "litellm": {"model_list": [model("shared", shared.json()["id"])]},
        "policy": {"research_model": {"editable": True}}, "quotas": {"monthly_cost_micro_usd": 1000000, "max_run_cost_micro_usd": 1000000}}
    draft = await client.post("/platform/configurations", json={"name": "平台模型", "scope": "global", "document": document})
    assert draft.status_code == 201, draft.text
    baseline = await client.post(f"/platform/configurations/{draft.json()['id']}/publish", json={"expected_version": 1, "reason": "授权测试"})
    assert baseline.status_code == 200, baseline.text
    platform_id = baseline.json()["id"]
    developer = await user_token(client, "platform-developer@example.com", "developer")
    researcher = await user_token(client, "platform-researcher@example.com", "researcher")
    client.headers["Authorization"] = developer
    private = await client.post("/platform/credentials", json={"name": "private", "scope": "personal", "provider": "openai", "secret": "private-test-placeholder"})
    assert private.status_code == 201, private.text
    rejected = await client.post("/platform/configurations", json={"name": "越权", "scope": "global", "document": {}})
    assert rejected.status_code == 403
    own = await client.post("/platform/configurations", json={"name": "个人模型", "scope": "personal", "document": {
        "runtime": {"research_model": "private"}, "litellm": {"model_list": [model("private", private.json()["id"])]}}})
    assert own.status_code == 201, own.text
    published = await client.post(f"/platform/configurations/{own.json()['id']}/publish", json={"expected_version": 1, "expected_platform_revision": platform_id, "reason": "个人调试"})
    assert published.status_code == 200, published.text
    route = model_aliases(published.json()["document"])["private"]
    preview = await client.post("/platform/configuration-preview", json={"profile_revision_id": published.json()["id"]})
    assert preview.status_code == 200, preview.text
    assert preview.json()["configurable"]["research_model"] == route
    client.headers["Authorization"] = researcher
    forbidden = await client.post("/platform/configuration-preview", json={"configurable": {"research_model": route}})
    assert forbidden.status_code == 403, forbidden.text
    assert (await client.get(f"/platform/configurations/{own.json()['id']}")).status_code == 404
    client.headers["Authorization"] = admin_token
    assert (await client.post(f"/platform/credentials/{private.json()['id']}/disable")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("native_backend", ["sqlite", "postgres"])
async def test_native_receipts_monthly_budget_and_history_share_one_charge(client, tmp_path, monkeypatch, native_backend):
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
    from open_deep_research.platform.history import import_native_usage
    user = await login(client)
    await publish(client)
    native = RecoveryStore(TEST_URL if native_backend == "postgres" else "sqlite+aiosqlite:///" + (tmp_path / "native.db").as_posix())
    await native.create_tables()
    try:
        run = ResearchSnapshot(run_id=uuid4().hex, config_fingerprint="fixture")
        await native.create_run(user["id"], run, limits={"cost_micro_usd": 100})
        lease = await native.acquire(run.run_id, user["id"])
        payload = {"model": "test"}
        await native.begin_operation(lease, "one", "model_attempt", payload, reserve={"cost_micro_usd": 60}, observation={"model": "test"})
        with pytest.raises(quota.QuotaExceeded):
            await native.begin_operation(lease, "blocked", "model_attempt", payload, reserve={"cost_micro_usd": 60}, observation={"model": "test"})
        await native.commit_operation(lease, "one", {"status": "completed", "cost_status": "reported"}, actual={"cost_micro_usd": 40})
        replay = await native.begin_operation(lease, "one", "model_attempt", payload, reserve={"cost_micro_usd": 60}, observation={"model": "test"})
        assert replay["replayed"] is True
        await native.begin_operation(lease, "unknown", "model_attempt", payload, reserve={"cost_micro_usd": 50}, observation={"model": "test"})
        await native.commit_operation(lease, "unknown", {"status": "failed", "cost_status": "unknown", "uncertain": True}, actual={"cost_micro_usd": 50})
        async with get_store().engine.connect() as conn:
            month = (await conn.execute(select(s.months))).mappings().one()
            assert (month["settled"], month["reserved"]) == (40, 50)
        await native.reconcile_model_usage(lease, "unknown", receipt_id="verified-fixture", input_tokens=0, output_tokens=0, cost_micro_usd=25)
        monkeypatch.setenv("PLATFORM_CONFIGURATION_MODE", "explicit")
        historic = ResearchSnapshot(run_id=uuid4().hex, config_fingerprint="legacy-fixture")
        await native.create_run(user["id"], historic, limits={"cost_micro_usd": 20})
        old = await native.acquire(historic.run_id, user["id"])
        await native.begin_operation(old, "legacy", "model_attempt", payload, reserve={"cost_micro_usd": 10}, observation={"model": "test"})
        await native.commit_operation(old, "legacy", {"status": "completed", "cost_status": "reported"}, actual={"cost_micro_usd": 10})
        monkeypatch.setenv("PLATFORM_CONFIGURATION_MODE", "database")
        result = await import_native_usage(native)
        assert result["imported"] == 1
        assert (await import_native_usage(native))["imported"] == 0
        async with get_store().engine.connect() as conn:
            month = (await conn.execute(select(s.months))).mappings().one()
            assert (month["settled"], month["reserved"]) == (75, 0)
    finally:
        if native_backend == "postgres":
            async with native.engine.begin() as conn:
                await conn.run_sync(native.meta.drop_all)
        await native.aclose()
