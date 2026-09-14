"""Per-Run LiteLLM Virtual Key security and budget tests."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, replace

import httpx
import pytest

from open_deep_research.models.credentials import (
    LiteLLMKeyAdminClient,
    LiteLLMTeamAdminClient,
    RunKeyLease,
    RunKeyManager,
    RunKeyMetadata,
    RunKeySecretStore,
    RunKeySettings,
    TeamBudgetPolicy,
    key_digest,
    micro_usd_to_usd,
    resolve_role_team_role,
    spend_micro_usd_from_key_info,
    team_budget_policies_from_env,
)


@dataclass
class FakeAdmin:
    blocked: list[str]
    fail_block: bool = False
    fail_renew: bool = False
    _generated: int = 0
    generated_team_ids: list = None

    def __post_init__(self) -> None:
        if self.generated_team_ids is None:
            self.generated_team_ids = []

    async def generate(self, *, run_id, budget_micro_usd, allowed_models, team_id=None):
        self._generated += 1
        self.generated_team_ids.append(team_id)
        key = "sk-run-secret" if self._generated == 1 else f"sk-run-secret-{self._generated}"
        return RunKeyLease(
            key=key,
            metadata=RunKeyMetadata(
                key_hash=key_digest(key),
                key_alias=f"run-{run_id}",
                budget_micro_usd=budget_micro_usd,
                expires_at=time.time() + 3600,
                allowed_models=allowed_models,
                policy_revision="v1",
                team_id=team_id,
            ),
        )

    async def renew(self, lease):
        if self.fail_renew:
            raise RuntimeError("key blocked or expired")
        return lease

    async def block(self, key):
        if self.fail_block:
            raise RuntimeError("offline")
        self.blocked.append(key)

    async def info(self, key):
        return {"info": {"spend": "1.234567"}}


def settings() -> RunKeySettings:
    return RunKeySettings(
        base_url="http://litellm-proxy:4000/v1",
        master_key="master",
        team_id="insightforge-runs",
        encryption_key=b"k" * 32,
        default_budget_micro_usd=2_000_000,
        maximum_budget_micro_usd=5_000_000,
    )


def test_budget_conversion_and_admin_ceiling() -> None:
    assert str(micro_usd_to_usd(1_234_567)) == "1.234567"
    assert settings().resolve_budget(None) == 2_000_000
    assert settings().resolve_budget(9_000_000) == 5_000_000
    assert spend_micro_usd_from_key_info({"info": {"spend": "1.234567"}}) == 1_234_567


def test_generate_service_key_sends_budget_and_allowlist() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"key": "sk-service-secret"})

    client = LiteLLMKeyAdminClient(
        settings(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://litellm-proxy:4000",
            headers={"Authorization": "Bearer master"},
        ),
    )

    async def run() -> str:
        try:
            return await client.generate_service_key(
                alias="insightforge-service",
                models=["if-evaluation-v1", "if-evaluation-v1"],
                max_budget_usd=20.0,
                budget_duration="30d",
            )
        finally:
            await client.aclose()

    key = asyncio.run(run())

    assert key == "sk-service-secret"
    assert captured["path"] == "/key/generate"
    assert captured["auth"] == "Bearer master"
    body = captured["body"]
    assert body["key_alias"] == "insightforge-service"
    assert body["models"] == ["if-evaluation-v1"]
    assert body["max_budget"] == 20.0
    assert body["budget_duration"] == "30d"
    assert body["team_id"] == "insightforge-runs"
    assert body["metadata"]["purpose"] == "service"
    assert "duration" not in body  # service keys never expire


def test_secret_store_encrypts_and_round_trips(tmp_path) -> None:
    store = RunKeySecretStore(tmp_path, b"k" * 32)
    store.save("run-1", "sk-run-secret")
    ciphertext = (tmp_path / "run-1" / "secrets" / "litellm-run-key.enc").read_bytes()
    assert b"sk-run-secret" not in ciphertext
    assert store.load("run-1") == "sk-run-secret"
    assert store.run_ids() == ["run-1"]


@pytest.mark.asyncio
async def test_manager_blocks_then_deletes_local_secret(tmp_path) -> None:
    store = RunKeySecretStore(tmp_path, b"k" * 32)
    admin = FakeAdmin([])
    manager = RunKeyManager(settings(), store, admin)  # type: ignore[arg-type]
    lease = await manager.ensure(
        run_id="run-1",
        requested_budget_micro_usd=None,
        allowed_models=["if-research-v1"],
    )
    assert lease.key not in lease.metadata.model_dump_json()
    assert await manager.authoritative_spend_micro_usd("run-1") == 1_234_567
    assert await manager.finalize("run-1") is True
    assert admin.blocked == ["sk-run-secret"]
    assert store.load("run-1") is None


@pytest.mark.asyncio
async def test_failed_remote_block_keeps_encrypted_secret(tmp_path) -> None:
    store = RunKeySecretStore(tmp_path, b"k" * 32)
    store.save("run-1", "sk-run-secret")
    manager = RunKeyManager(
        settings(),
        store,
        FakeAdmin([], fail_block=True),  # type: ignore[arg-type]
    )
    assert await manager.finalize("run-1") is False
    assert store.load("run-1") == "sk-run-secret"


@pytest.mark.asyncio
async def test_resume_regenerates_when_secret_or_metadata_is_lost(tmp_path) -> None:
    """A lost secret (reconciler cleanup, crash) must not break resume."""
    store = RunKeySecretStore(tmp_path, b"k" * 32)
    admin = FakeAdmin([])
    manager = RunKeyManager(settings(), store, admin)  # type: ignore[arg-type]
    metadata = RunKeyMetadata(
        key_hash=key_digest("sk-run-secret"),
        key_alias="run-run-1",
        budget_micro_usd=2_000_000,
        expires_at=time.time() + 3600,
        allowed_models=["if-research-v1"],
        policy_revision="v1",
    )

    # Secret missing but manifest metadata retained: mint a replacement that
    # preserves the run's original budget instead of raising.
    lease = await manager.ensure(
        run_id="run-1",
        requested_budget_micro_usd=None,
        allowed_models=["if-research-v1"],
        metadata=metadata,
    )
    assert lease.key == "sk-run-secret"
    assert lease.metadata.budget_micro_usd == 2_000_000
    assert store.load("run-1") == "sk-run-secret"

    # Metadata missing (persistence disabled between messages): block the
    # unattributable stored key and continue with a fresh one.
    store.save("run-2", "sk-orphan")
    lease = await manager.ensure(
        run_id="run-2",
        requested_budget_micro_usd=None,
        allowed_models=["if-research-v1"],
    )
    assert admin.blocked == ["sk-orphan"]
    assert lease.key == "sk-run-secret-2"
    assert store.load("run-2") == lease.key


@pytest.mark.asyncio
async def test_resume_regenerates_when_renewal_is_rejected(tmp_path) -> None:
    """A key the gateway refuses to renew (blocked/expired) is replaced."""
    store = RunKeySecretStore(tmp_path, b"k" * 32)
    store.save("run-1", "sk-run-secret")
    admin = FakeAdmin([], fail_renew=True)
    manager = RunKeyManager(settings(), store, admin)  # type: ignore[arg-type]
    metadata = RunKeyMetadata(
        key_hash=key_digest("sk-run-secret"),
        key_alias="run-run-1",
        budget_micro_usd=2_000_000,
        expires_at=time.time() - 1,
        allowed_models=["if-research-v1"],
        policy_revision="v1",
    )

    lease = await manager.ensure(
        run_id="run-1",
        requested_budget_micro_usd=None,
        allowed_models=["if-research-v1"],
        metadata=metadata,
    )

    assert admin.blocked == ["sk-run-secret"]
    assert lease.key == "sk-run-secret"
    assert store.load("run-1") == "sk-run-secret"


@pytest.mark.asyncio
async def test_resume_uses_manifest_budget_when_admin_default_changes(tmp_path) -> None:
    store = RunKeySecretStore(tmp_path, b"k" * 32)
    store.save("run-1", "sk-run-secret")
    changed_settings = replace(
        settings(),
        default_budget_micro_usd=3_000_000,
    )
    manager = RunKeyManager(
        changed_settings,
        store,
        FakeAdmin([]),  # type: ignore[arg-type]
    )
    metadata = RunKeyMetadata(
        key_hash=key_digest("sk-run-secret"),
        key_alias="run-run-1",
        budget_micro_usd=2_000_000,
        expires_at=time.time() + 3600,
        allowed_models=["if-research-v1"],
        policy_revision="v1",
    )

    lease = await manager.ensure(
        run_id="run-1",
        requested_budget_micro_usd=None,
        allowed_models=["if-research-v1"],
        metadata=metadata,
    )

    assert lease.metadata.budget_micro_usd == 2_000_000


def test_team_budget_policies_from_env_matrix(monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith("LITELLM_TEAM_"):
            monkeypatch.delenv(name, raising=False)

    assert team_budget_policies_from_env() == {}

    monkeypatch.setenv("LITELLM_TEAM_DEFAULT_BUDGET_MICRO_USD", "1000000")
    policies = team_budget_policies_from_env()
    assert set(policies) == {"admin", "developer", "researcher"}
    assert policies["researcher"].budget_micro_usd == 1_000_000
    assert policies["researcher"].budget_duration == "30d"
    assert policies["researcher"].rpm_limit is None

    monkeypatch.setenv("LITELLM_TEAM_DEVELOPER_BUDGET_MICRO_USD", "5000000")
    monkeypatch.setenv("LITELLM_TEAM_DEVELOPER_BUDGET_DURATION", "7d")
    monkeypatch.setenv("LITELLM_TEAM_DEVELOPER_RPM_LIMIT", "600")
    policies = team_budget_policies_from_env()
    assert policies["developer"].budget_micro_usd == 5_000_000
    assert policies["developer"].budget_duration == "7d"
    assert policies["developer"].rpm_limit == 600
    assert policies["researcher"].budget_duration == "30d"

    monkeypatch.setenv("LITELLM_TEAM_ADMIN_BUDGET_DURATION", "not-a-duration")
    with pytest.raises(Exception, match="BUDGET_DURATION"):
        team_budget_policies_from_env()
    monkeypatch.setenv("LITELLM_TEAM_ADMIN_BUDGET_DURATION", "30d")
    monkeypatch.setenv("LITELLM_TEAM_ADMIN_BUDGET_MICRO_USD", "0")
    with pytest.raises(Exception, match="positive"):
        team_budget_policies_from_env()


def test_resolve_role_team_role_priority_and_local_dev_exempt() -> None:
    policies = {
        "admin": TeamBudgetPolicy(budget_micro_usd=100),
        "developer": TeamBudgetPolicy(budget_micro_usd=50),
        "researcher": TeamBudgetPolicy(budget_micro_usd=20),
    }
    assert resolve_role_team_role(["researcher"], user_id="u-1", policies=policies) == "researcher"
    assert (
        resolve_role_team_role(["researcher", "developer"], user_id="u-1", policies=policies)
        == "developer"
    )
    assert (
        resolve_role_team_role(["developer", "admin"], user_id="u-1", policies=policies)
        == "admin"
    )
    assert resolve_role_team_role(["viewer"], user_id="u-1", policies=policies) is None
    assert resolve_role_team_role([], user_id="u-1", policies=policies) is None
    # Synthetic local-dev identity never joins role teams.
    assert (
        resolve_role_team_role(["admin"], user_id="local-dev-user", policies=policies)
        is None
    )
    # A role without a configured budget falls back down the priority ladder.
    assert (
        resolve_role_team_role(
            ["admin"], user_id="u-1", policies={"researcher": policies["researcher"]}
        )
        is None
    )


def test_team_admin_client_creates_updates_and_caches(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    state = {
        "teams": [
            {
                "team_id": "team-dev",
                "team_alias": "if-team-developer",
                "max_budget": 5.0,
                "budget_duration": "30d",
                "rpm_limit": 600,
            }
        ]
    }
    monkeypatch.setattr(LiteLLMTeamAdminClient, "_team_id_cache", {})

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        calls.append((request.url.path, body))
        if request.url.path == "/team/list":
            return httpx.Response(200, json={"teams": state["teams"]})
        if request.url.path == "/team/new":
            state["teams"].append(
                {
                    "team_id": "team-new",
                    "team_alias": body["team_alias"],
                    "max_budget": body.get("max_budget"),
                    "budget_duration": body.get("budget_duration"),
                }
            )
            return httpx.Response(200, json={"team_id": "team-new"})
        if request.url.path == "/team/update":
            for team in state["teams"]:
                if team["team_id"] == body["team_id"]:
                    team.update(
                        max_budget=body.get("max_budget"),
                        budget_duration=body.get("budget_duration"),
                    )
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected path {request.url.path}")

    def make_client() -> LiteLLMTeamAdminClient:
        return LiteLLMTeamAdminClient(
            settings(),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://litellm-proxy:4000",
                headers={"Authorization": "Bearer master"},
            ),
        )

    matching = TeamBudgetPolicy(budget_micro_usd=5_000_000, budget_duration="30d", rpm_limit=600)
    changed = TeamBudgetPolicy(budget_micro_usd=8_000_000, budget_duration="7d")
    fresh = TeamBudgetPolicy(budget_micro_usd=1_000_000)

    async def run() -> None:
        # Existing team with a matching policy: resolve only, no update.
        client = make_client()
        try:
            assert await client.ensure_team("if-team-developer", matching) == "team-dev"
        finally:
            await client.aclose()
        assert [path for path, _body in calls] == ["/team/list"]

        # Same alias again hits the process cache without any HTTP call.
        client = make_client()
        try:
            assert await client.ensure_team("if-team-developer", matching) == "team-dev"
        finally:
            await client.aclose()
        assert len(calls) == 1

        # Diverging policy converges the remote team via /team/update.
        client = make_client()
        monkeypatch.setattr(LiteLLMTeamAdminClient, "_team_id_cache", {})
        try:
            assert await client.ensure_team("if-team-developer", changed) == "team-dev"
        finally:
            await client.aclose()
        assert [path for path, _body in calls][-1] == "/team/update"

        # Unknown alias is created via /team/new.
        monkeypatch.setattr(LiteLLMTeamAdminClient, "_team_id_cache", {})
        client = make_client()
        try:
            assert await client.ensure_team("if-team-researcher", fresh) == "team-new"
        finally:
            await client.aclose()
        new_call = [body for path, body in calls if path == "/team/new"][0]
        assert new_call["team_alias"] == "if-team-researcher"
        assert new_call["max_budget"] == 1.0

    asyncio.run(run())


def test_run_key_manager_pins_role_team_on_generate(tmp_path) -> None:
    admin = FakeAdmin(blocked=[])
    manager = RunKeyManager(settings(), RunKeySecretStore(tmp_path, b"k" * 32), admin)

    async def run() -> None:
        lease = await manager.ensure(
            run_id="run-team",
            requested_budget_micro_usd=None,
            allowed_models=["if-research-v1"],
            team_id="team-developer",
        )
        assert lease.metadata.team_id == "team-developer"
        assert admin.generated_team_ids == ["team-developer"]

        # Without a role team the key keeps the global Run team.
        await manager.ensure(
            run_id="run-global",
            requested_budget_micro_usd=None,
            allowed_models=["if-research-v1"],
        )
        assert admin.generated_team_ids == ["team-developer", None]

    asyncio.run(run())
