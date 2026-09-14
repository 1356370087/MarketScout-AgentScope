"""Run Key reconciliation must never clean a resumable (paused) Run."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import open_deep_research.models.key_reconciler as reconciler_module
from open_deep_research.models.credentials import (
    RunKeySecretStore,
    RunKeySettings,
)
from open_deep_research.models.key_reconciler import reconcile_run_keys_once


def settings() -> RunKeySettings:
    return RunKeySettings(
        base_url="http://litellm-proxy:4000/v1",
        master_key="master",
        team_id="insightforge-runs",
        encryption_key=b"k" * 32,
        default_budget_micro_usd=2_000_000,
        maximum_budget_micro_usd=5_000_000,
    )


@dataclass
class FakeAdmin:
    blocked: list[str] = field(default_factory=list)
    remote_entries: list[dict] = field(default_factory=list)

    async def generate(self, *, run_id, budget_micro_usd, allowed_models):
        raise AssertionError("reconciler must not generate keys")

    async def renew(self, lease):
        return lease

    async def block(self, key):
        self.blocked.append(key)

    async def block_entry(self, entry):
        self.blocked.append(str(entry.get("key") or entry.get("token")))

    async def info(self, key):
        return {"info": {"spend": "2.000000"}}

    async def list_run_keys(self, *, page_size=100, max_keys=1000):
        return self.remote_entries

    async def aclose(self):
        return None


def _write_run(
    runs_dir: Path,
    run_id: str,
    *,
    status: str,
    fence_token: int = 1,
    lease_active: bool = False,
) -> None:
    run_dir = runs_dir / run_id
    (run_dir / "context").mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "run_id": run_id,
        "status": status,
        "fence_token": fence_token,
    }
    (run_dir / "context" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    coordination = run_dir / "coordination"
    coordination.mkdir(parents=True, exist_ok=True)
    (coordination / "leader_lease.json").write_text(
        json.dumps(
            {
                "owner_instance_id": "owner",
                "fence_token": fence_token,
                "lease_expires_at": time.time() + 300 if lease_active else 0,
            }
        ),
        encoding="utf-8",
    )


def _write_secret(runs_dir: Path, run_id: str) -> None:
    RunKeySecretStore(runs_dir, b"k" * 32).save(run_id, f"sk-{run_id}")


async def _run_reconciler(tmp_path: Path, remote_entries=None) -> tuple[dict, FakeAdmin]:
    admin = FakeAdmin(remote_entries=remote_entries or [])
    original_client = reconciler_module.LiteLLMKeyAdminClient

    class PatchedAdminClient:
        def __init__(self, _settings) -> None:
            pass

        def __getattr__(self, name):
            return getattr(admin, name)

    reconciler_module.LiteLLMKeyAdminClient = PatchedAdminClient  # type: ignore[assignment]
    try:
        result = await reconcile_run_keys_once(settings(), runs_dir=str(tmp_path))
    finally:
        reconciler_module.LiteLLMKeyAdminClient = original_client  # type: ignore[assignment]
    return result, admin


@pytest.mark.asyncio
async def test_paused_run_is_never_cleaned(tmp_path) -> None:
    """HITL pause releases the lease but must not release the key."""
    _write_run(tmp_path, "paused-run", status="awaiting_plan_approval", lease_active=False)
    _write_secret(tmp_path, "paused-run")

    result, admin = await _run_reconciler(tmp_path)

    assert result == {
        "checked": 1,
        "cleaned": 0,
        "failed": 0,
        "stale": 0,
        "orphan_blocked": 0,
    }
    assert admin.blocked == []
    assert RunKeySecretStore(tmp_path, b"k" * 32).load("paused-run") == "sk-paused-run"


@pytest.mark.asyncio
async def test_running_run_with_dead_lease_is_flagged_not_cleaned(tmp_path) -> None:
    _write_run(tmp_path, "crashed-run", status="running", lease_active=False)
    _write_secret(tmp_path, "crashed-run")

    result, admin = await _run_reconciler(tmp_path)

    assert result["stale"] == 1
    assert result["cleaned"] == 0
    assert admin.blocked == []


@pytest.mark.asyncio
async def test_running_run_with_active_lease_is_untouched(tmp_path) -> None:
    _write_run(tmp_path, "live-run", status="running", lease_active=True)
    _write_secret(tmp_path, "live-run")

    result, admin = await _run_reconciler(tmp_path)

    assert result["stale"] == 0
    assert result["cleaned"] == 0
    assert admin.blocked == []


@pytest.mark.asyncio
async def test_terminal_run_is_cleaned_and_bookkeeping_written_past_fence(tmp_path) -> None:
    """Spend writeback must succeed for fenced manifests (maintenance path)."""
    _write_run(tmp_path, "done-run", status="completed", lease_active=False)
    _write_secret(tmp_path, "done-run")

    result, admin = await _run_reconciler(tmp_path)

    assert result["cleaned"] == 1
    assert admin.blocked == ["sk-done-run"]
    manifest = json.loads(
        (tmp_path / "done-run" / "context" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["litellm_spend_micro_usd"] == 2_000_000
    assert manifest["litellm_key_cleanup_pending"] is False


@pytest.mark.asyncio
async def test_remote_orphan_keys_are_blocked_after_grace(tmp_path) -> None:
    old = time.time() - 3600
    entries = [
        # Old key whose secret was never persisted locally: block.
        {
            "key": "sk-orphan",
            "metadata": {"run_id": "lost-run"},
            "created_at": old,
        },
        # Freshly generated key may still be mid-persist: skip.
        {
            "key": "sk-fresh",
            "metadata": {"run_id": "new-run"},
            "created_at": time.time(),
        },
        # Key whose run still holds a local secret: governed by run status.
        {
            "key": "sk-live",
            "metadata": {"run_id": "live-run"},
            "created_at": old,
        },
        # Unrelated team keys without run metadata: skip.
        {"key": "sk-service", "key_alias": "service-key", "created_at": old},
        # Alias-only entry with unknown age: skip (conservative).
        {"key": "sk-alias-only", "key_alias": "run-aliasrun-ab12cd34"},
    ]
    _write_run(tmp_path, "live-run", status="running", lease_active=True)
    _write_secret(tmp_path, "live-run")

    result, admin = await _run_reconciler(tmp_path, remote_entries=entries)

    assert admin.blocked == ["sk-orphan"]
    assert result["orphan_blocked"] == 1
