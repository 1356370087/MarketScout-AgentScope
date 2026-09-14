"""Background cleanup for orphaned or terminal LiteLLM Run Keys."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from open_deep_research.models.credentials import (
    LiteLLMKeyAdminClient,
    RunKeyManager,
    RunKeySecretStore,
    RunKeySettings,
)
from open_deep_research.run_context import RunContextStore

logger = logging.getLogger(__name__)
_TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
# Non-terminal statuses that describe a healthy paused Run awaiting a human;
# their keys must survive until the Run resumes or its remote TTL expires.
_PAUSED = frozenset(
    {"awaiting_clarification", "awaiting_plan_approval", "awaiting_outline_approval", "awaiting_fetch_budget_approval"}
)
# Remote keys younger than this are never orphan-blocked: a Run that just
# generated a key may not have persisted its encrypted secret yet.
_ORPHAN_GRACE_SECONDS = 600.0


def _lease_is_active(runs_dir: str, run_id: str) -> bool:
    path = Path(runs_dir) / run_id / "coordination" / "leader_lease.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return float(payload.get("lease_expires_at", 0)) > time.time()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _entry_created_at(entry: dict[str, Any]) -> float | None:
    raw = entry.get("created_at") or entry.get("created")
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _entry_run_id(entry: dict[str, Any]) -> str | None:
    metadata = entry.get("metadata")
    if isinstance(metadata, dict):
        run_id = metadata.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    alias = entry.get("key_alias")
    if isinstance(alias, str) and alias.startswith("run-"):
        # run-{run_id}-{hex}; strip the two suffix segments appended at generate.
        parts = alias.split("-")
        if len(parts) >= 4:
            return "-".join(parts[1:-2])
    return None


async def reconcile_run_keys_once(settings: RunKeySettings, *, runs_dir: str) -> dict[str, int]:
    """Block encrypted keys whose Run is terminal, plus remote orphaned keys.

    Only terminal Runs release their keys: paused and crashed Runs keep theirs
    (resume regenerates a replacement when reconciliation has cleaned up, and
    the remote TTL bounds anything abandoned). Runs with a dead lease but no
    local secret can never use their key again, so a listed remote key without
    a local secret is blocked after the orphan grace window.
    """
    store = RunKeySecretStore(runs_dir, settings.encryption_key)
    admin = LiteLLMKeyAdminClient(settings)
    manager = RunKeyManager(settings, store, admin)
    checked = cleaned = failed = stale = orphan_blocked = 0
    try:
        for run_id in store.run_ids():
            checked += 1
            context = RunContextStore(run_id, runs_dir=runs_dir)
            try:
                manifest = context.load_manifest()
            except (OSError, ValueError):
                manifest = None
            status = manifest.status if manifest is not None else None
            should_clean = manifest is None or status in _TERMINAL
            if not should_clean:
                if status not in _PAUSED and not _lease_is_active(runs_dir, run_id):
                    stale += 1
                continue
            spend: int | None = None
            try:
                spend = await manager.authoritative_spend_micro_usd(run_id)
            except Exception:  # noqa: BLE001 - cleanup remains the priority
                pass
            if not await manager.finalize(run_id):
                failed += 1
                continue
            cleaned += 1
            if manifest is not None:
                try:
                    updates: dict[str, object] = {"litellm_key_cleanup_pending": False}
                    if spend is not None:
                        updates.update(
                            litellm_spend_micro_usd=spend,
                            litellm_spend_updated_at=time.time(),
                        )
                    context.update_reconciliation_fields(**updates)
                except Exception:  # noqa: BLE001 - bookkeeping must not block cleanup
                    logger.debug("Run Key bookkeeping skipped for %s", run_id)
        orphan_blocked = await _block_remote_orphan_keys(store, admin)
    finally:
        await admin.aclose()
    return {
        "checked": checked,
        "cleaned": cleaned,
        "failed": failed,
        "stale": stale,
        "orphan_blocked": orphan_blocked,
    }


async def _block_remote_orphan_keys(
    store: RunKeySecretStore,
    admin: LiteLLMKeyAdminClient,
) -> int:
    """Block listed team keys whose plaintext secret no longer exists locally.

    Without the encrypted secret no caller can ever present the key, so
    blocking is safe for any Run state; the grace window keeps a Run that is
    between remote generation and local persistence out of the blast radius.
    """
    blocked = 0
    try:
        entries = await admin.list_run_keys()
    except Exception:  # noqa: BLE001 - remote listing is best-effort
        return 0
    known_secret_runs = set(store.run_ids())
    for entry in entries:
        run_id = _entry_run_id(entry)
        if run_id is None or run_id in known_secret_runs:
            continue
        if entry.get("blocked") is True:
            continue
        created_at = _entry_created_at(entry)
        if created_at is None or time.time() - created_at < _ORPHAN_GRACE_SECONDS:
            continue
        try:
            await admin.block_entry(entry)
        except Exception:  # noqa: BLE001 - retry on the next cycle
            continue
        blocked += 1
    return blocked


async def run_key_reconciler_loop(
    settings: RunKeySettings,
    *,
    runs_dir: str,
    interval_seconds: float,
) -> None:
    """Periodically retry Run Key cleanup until application shutdown."""
    while True:
        try:
            await reconcile_run_keys_once(settings, runs_dir=runs_dir)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconciler is fail-open
            logger.warning("LiteLLM Run Key reconciliation failed: %s", type(exc).__name__)
        await asyncio.sleep(max(10.0, interval_seconds))


__all__ = ["reconcile_run_keys_once", "run_key_reconciler_loop"]
