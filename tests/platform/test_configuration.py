"""控制面的版本、归属、凭据及并发费用契约。"""

import asyncio
import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from open_deep_research.platform import schema as s
from open_deep_research.platform.contracts import ConfigurationDocument, DraftRequest, PublishRequest, UpdateDraft, CredentialRequest, UserQuotaLimits
from open_deep_research.platform.crypto import CredentialCipher
from open_deep_research.platform.store import PlatformStore, PlatformError
from open_deep_research.platform import quota
from security.rbac.platform_permissions import ADMIN_GRANTS, DEVELOPER_GRANTS


def principal(permissions=ADMIN_GRANTS):
    return SimpleNamespace(user_id=str(uuid4()), permissions=frozenset(permissions), session_id=str(uuid4()))


@pytest_asyncio.fixture
async def store(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "platform.db").as_posix())
    async with engine.begin() as conn:
        await conn.run_sync(s.metadata.create_all)
        await conn.execute(insert(s.state).values(id=1, initialized=True, generation=0))
    yield PlatformStore(engine, CredentialCipher(base64.urlsafe_b64encode(b"x" * 32).decode()))
    await engine.dispose()


async def activate(store, user, amount=100):
    document = ConfigurationDocument(runtime={"research_model": "openai:test", "max_researcher_iterations": 4,
        "model_costs_per_million": {"openai:test": {"input": 1.0, "output": 1.0}}},
        services={"PUBLISHER_ENABLED": False},
        quotas={"monthly_cost_micro_usd": amount, "max_run_cost_micro_usd": 100},
        policy={"max_researcher_iterations": {"editable": True, "minimum": 1, "maximum": 8}})
    draft = await store.create_draft(user, DraftRequest(name="平台", scope="global", document=document))
    row = await store.publish(user, draft["id"], PublishRequest(expected_version=1, reason="测试发布"))
    await store.application(row["id"], "api", "applied", {})
    await store.activate(user, row["id"])
    return row


@pytest.mark.asyncio
async def test_published_revision_is_immutable_and_activation_checks_components(store):
    user = principal()
    row = await activate(store, user)
    with pytest.raises(PlatformError, match="version_conflict"):
        await store.update_draft(user, row["id"], UpdateDraft(expected_version=2, document=ConfigurationDocument()))
    active = await store.active()
    assert active["id"] == row["id"]
    async with store.engine.connect() as conn:
        assert len((await conn.execute(select(s.audit))).all()) == 3


@pytest.mark.asyncio
async def test_personal_owner_and_global_policy_are_enforced(store):
    admin, developer, other = principal(), principal(DEVELOPER_GRANTS), principal(DEVELOPER_GRANTS)
    await activate(store, admin)
    row = await store.create_draft(developer, DraftRequest(name="调试", document=ConfigurationDocument(runtime={"max_researcher_iterations": 6})))
    with pytest.raises(PlatformError, match="not_found"):
        await store.revision(row["id"], other)
    with pytest.raises(PlatformError, match="locked"):
        await store.create_draft(developer, DraftRequest(name="越权", document=ConfigurationDocument(runtime={"research_model": "openai:other"})))
    with pytest.raises(PlatformError, match="bound_violated"):
        await store.create_draft(developer, DraftRequest(name="超限", document=ConfigurationDocument(runtime={"max_researcher_iterations": 9})))


@pytest.mark.asyncio
async def test_secret_is_encrypted_bound_to_identity_and_never_exported(store):
    admin = principal()
    public = await store.create_credential(admin, CredentialRequest(name="测试", provider="openai", scope="global", secret="secret-example-only"))
    assert "ciphertext" not in public
    row = await store.credential(public["id"])
    assert b"secret-example-only" not in row["ciphertext"]
    assert await store.secret(public["id"], admin.user_id) == "secret-example-only"
    with pytest.raises(Exception):
        store.cipher.decrypt("other:1:global", row["ciphertext"])


@pytest.mark.asyncio
async def test_concurrent_monthly_reservation_and_idempotent_settlement(store):
    user = principal()
    await activate(store, user, amount=100)
    results = await asyncio.gather(*(quota.reserve(user.user_id, "op-" + str(i), 60, "test", store=store) for i in range(2)), return_exceptions=True)
    assert sum(isinstance(result, quota.QuotaExceeded) for result in results) == 1
    accepted = next(result for result in results if isinstance(result, dict))
    await quota.settle(accepted["operation_id"], 40, store=store)
    await quota.settle(accepted["operation_id"], 40, store=store)
    await quota.reserve(user.user_id, "next", 60, "test", store=store)
    async with store.engine.connect() as conn:
        row = (await conn.execute(select(s.months))).mappings().one()
        assert row["settled"] == 40 and row["reserved"] == 60


@pytest.mark.asyncio
async def test_unlimited_user_keeps_accounting_isolation_and_other_limits(store):
    user, other = principal(), principal()
    await activate(store, user, amount=100)
    limits = UserQuotaLimits(monthly_cost_unlimited=True)
    async with store.engine.begin() as conn:
        await conn.execute(insert(s.quotas).values(owner_id=user.user_id, limits=limits.model_dump(), version=1))
    effective = await store.limits(user.user_id)
    assert effective["monthly_cost_unlimited"] is True
    assert effective["max_run_cost_micro_usd"] == 100
    await asyncio.gather(*(quota.reserve(user.user_id, "unlimited-" + str(i), 600, "test", store=store) for i in range(3)))
    await quota.settle("unlimited-0", 400, store=store)
    await quota.settle("unlimited-0", 400, store=store)
    await quota.mark_unknown("unlimited-1", store=store)
    with pytest.raises(quota.QuotaExceeded):
        await quota.reserve(other.user_id, "other-owner", 600, "test", store=store)
    async with store.engine.connect() as conn:
        month = (await conn.execute(select(s.months).where(s.months.c.owner_id == user.user_id))).mappings().one()
        assert (month["reserved"], month["settled"]) == (1200, 400)
    # Re-enabling the monthly ceiling preserves prior spend and unknown calls.
    async with store.engine.begin() as conn:
        await conn.execute(update(s.quotas).where(s.quotas.c.owner_id == user.user_id)
            .values(limits=UserQuotaLimits().model_dump()))
    with pytest.raises(quota.QuotaExceeded):
        await quota.reserve(user.user_id, "after-reenable", 1, "test", store=store)
    await quota.settle("unlimited-2", 200, store=store)
    async with store.engine.connect() as conn:
        month = (await conn.execute(select(s.months).where(s.months.c.owner_id == user.user_id))).mappings().one()
        assert (month["reserved"], month["settled"]) == (600, 600)


def test_monthly_unlimited_is_an_explicit_user_override_not_a_global_default():
    assert UserQuotaLimits().monthly_cost_unlimited is False
    assert UserQuotaLimits(monthly_cost_micro_usd=None).monthly_cost_unlimited is False
    with pytest.raises(ValueError, match="monthly_cost_unlimited"):
        ConfigurationDocument(quotas={"monthly_cost_unlimited": True})


@pytest.mark.asyncio
async def test_unknown_retains_reservation_and_period_survives_month_boundary(store):
    user = principal()
    await activate(store, user)
    before = datetime(2026, 10, 31, 15, 59, tzinfo=timezone.utc)
    after = datetime(2026, 10, 31, 16, 1, tzinfo=timezone.utc)
    await quota.reserve(user.user_id, "old", 90, "test", store=store, at=before)
    await quota.mark_unknown("old", store=store)
    await quota.reserve(user.user_id, "new", 90, "test", store=store, at=after)
    await quota.settle("old", 50, store=store)
    async with store.engine.connect() as conn:
        rows = {row["period"]: dict(row) for row in (await conn.execute(select(s.months))).mappings()}
        assert rows["2026-10"]["settled"] == 50
        assert rows["2026-11"]["reserved"] == 90


def test_import_does_not_expand_shell_or_environment_and_redacts_secrets():
    from open_deep_research.platform.importing import parse_import, assert_secret_free
    document, secrets, report = parse_import("env", 'RESEARCH_MODEL="openai:test"\nOPENAI_API_KEY="secret-example"\nIAM_DATABASE_URL="deployment-only"\nUNKNOWN_VALUE=$(anything)')
    assert document["runtime"]["research_model"] == "openai:test"
    assert "secret-example" not in str(document)
    assert secrets["OPENAI_API_KEY"] == "secret-example"
    assert report["deployment_fields"] == ["IAM_DATABASE_URL"]
    assert report["unsupported_fields"] == ["UNKNOWN_VALUE"]
    with pytest.raises(ValueError, match="秘密"):
        assert_secret_free({"litellm_params": {"api_key": "literal-secret"}})


@pytest.mark.asyncio
async def test_released_operation_must_reserve_again_and_unknown_can_be_reconciled(store):
    user = principal()
    await activate(store, user, amount=100)
    await quota.reserve(user.user_id, "retry", 60, "test", store=store)
    await quota.release("retry", store=store)
    await quota.reserve(user.user_id, "other", 60, "test", store=store)
    with pytest.raises(quota.QuotaExceeded):
        await quota.reserve(user.user_id, "retry", 60, "test", store=store)
    await quota.mark_unknown("other", store=store)
    await quota.correct("other", 20, store=store)
    await quota.correct("other", 20, store=store)
    await quota.reserve(user.user_id, "retry", 60, "test", store=store)
    async with store.engine.connect() as conn:
        row = (await conn.execute(select(s.months))).mappings().one()
        assert (row["reserved"], row["settled"]) == (60, 20)


def test_database_snapshot_survives_environment_and_platform_changes(monkeypatch):
    from open_deep_research.platform import runtime
    from open_deep_research.agentscope_runtime.run_config import RunConfig
    monkeypatch.setenv("PLATFORM_CONFIGURATION_MODE", "database")
    monkeypatch.setenv("RESEARCH_MODEL", "openai:environment-must-not-win")
    old = {"revision_id": "old", "document": {"runtime": {"research_model": "openai:frozen", "enable_memory": False}, "services": {}}}
    runtime.install(old)
    try:
        run = RunConfig.compile({})
        runtime.install({"revision_id": "new", "document": {"runtime": {"research_model": "openai:changed", "enable_memory": True}, "services": {}}})
        restored = RunConfig.restore(run.snapshot())
        assert restored.get("research_model") == "openai:frozen"
        assert restored.get("enable_memory") is False
        assert restored.snapshot()["contract"]["metadata"]["platform_revision_id"] == "old"
    finally:
        runtime.reset()


@pytest.mark.asyncio
async def test_published_credential_reference_pins_secret_version(store):
    user = principal()
    credential = await store.create_credential(user, CredentialRequest(name="provider", provider="openai", scope="global", secret="old-test-secret"))
    document = ConfigurationDocument(runtime={"model_costs_per_million": {"openai:test": {"input": 1, "output": 1}}},
        services={"PUBLISHER_ENABLED": False}, credential_refs={"OPENAI_API_KEY": credential["id"]},
        quotas={"monthly_cost_micro_usd": 100, "max_run_cost_micro_usd": 100})
    draft = await store.create_draft(user, DraftRequest(name="凭据版本", scope="global", document=document))
    published = await store.publish(user, draft["id"], PublishRequest(expected_version=1, reason="锁定凭据版本"))
    assert published["document"]["credential_refs"]["OPENAI_API_KEY"] == "credential:" + credential["id"] + "@1"


def test_managed_execution_keeps_only_bootstrap_capability_environment(monkeypatch):
    from open_deep_research.platform import runtime
    monkeypatch.setenv("PLATFORM_CONFIGURATION_MODE", "database")
    monkeypatch.setenv("SANDBOX_TASK_TOKEN", "signed-task-fixture")
    monkeypatch.setenv("RESEARCH_MODEL", "environment-override")
    runtime.install({"revision_id": "fixture", "document": {"runtime": {"research_model": "database-model"}, "services": {}}})
    try:
        assert runtime.getenv("SANDBOX_TASK_TOKEN") == "signed-task-fixture"
        assert runtime.getenv("RESEARCH_MODEL") == "database-model"
    finally:
        runtime.reset()
