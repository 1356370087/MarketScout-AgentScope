"""Run-level usage accounting and native/history dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from open_deep_research.budgets import RunBudgetLedger
from open_deep_research.configuration import Configuration
from open_deep_research.models.credentials import (
    LiteLLMKeyAdminClient, LiteLLMKeyConfigurationError, RunKeyManager,
    RunKeySecretStore, RunKeySettings,
)
from open_deep_research.models.spend import LiteLLMSpendClient, aggregate_spend_logs_by_tag_prefix
from open_deep_research.observability import SQLiteTraceStore
from open_deep_research.run_context import JournalCorruptedError, RunContextStore
from open_deep_research.sandbox.internal_api import reconcile_run_gateway_usage
from security.rbac import Principal, require_permissions
from security.rbac.permissions import RESEARCH_RUN_READ_OWN

logger = logging.getLogger(__name__)


def _unavailable_usage_response(
    run_id: str,
    *,
    status: str = "unknown",
    configurable: Configuration,
    reason: str = "storage_unavailable",
) -> dict[str, Any]:
    vector = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 0,
    }
    limits = {
        "input_tokens": configurable.max_run_input_tokens,
        "output_tokens": configurable.max_run_output_tokens,
        "model_calls": configurable.max_run_model_calls,
        "cost_micro_usd": configurable.max_run_cost_micro_usd,
    }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": status,
        "duration_ms": None,
        "revision": 0,
        "updated_at": None,
        "accounting_status": "unavailable",
        "unavailable_reason": reason,
        "totals": {
            "reported": dict(vector),
            "estimated": dict(vector),
            "calls": {
                "attempts": 0,
                "successful_responses": 0,
                "provider_reported": 0,
                "provider_partial": 0,
                "estimated": 0,
                "missing": 0,
                "unknown_failed_attempts": 0,
                "legacy_unclassified": 0,
                "coverage_ratio": 0.0,
            },
            "cost": {
                "estimated_cost_micro_usd": None,
                "cost_source": "unavailable",
                "price_table_hash": None,
            },
            "budgets": {
                key: {
                    "settled": None if key == "cost_micro_usd" else 0,
                    "estimated": 0,
                    "reserved": 0,
                    "limit": limit,
                }
                for key, limit in limits.items()
            },
        },
        "breakdowns": {
            "by_stage": [],
            "by_agent_role": [],
            "by_model": [],
            "by_task": [],
        },
        "timeline": [],
        "operations": {
            "llm_call_count": 0,
            "retry_count": 0,
            "rate_limited_count": 0,
            "rate_429": 0.0,
            "cache_hit_rate": 0.0,
            "cache_input_ratio": 0.0,
            "reasoning_output_ratio": 0.0,
            "output_tokens_per_second": 0.0,
            "tool_call_count": 0,
            "tool_success_rate": 0.0,
            "empty_tool_result_count": 0,
            "zero_source_search_count": 0,
        },
    }


def _outstanding_usage_budget(
    configurable: Configuration,
    run_id: str,
) -> dict[str, int]:
    try:
        return RunBudgetLedger(
            run_id,
            runs_dir=configurable.runs_dir,
        ).outstanding_by_dimension()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        logger.warning(
            "Token budget ledger unavailable for run %s",
            run_id,
            exc_info=True,
        )
        return {}


def _load_run_usage_response(
    run_id: str,
    *,
    status: str,
    configurable: Configuration,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    if not configurable.token_usage_accounting_enabled:
        response = _unavailable_usage_response(
            run_id,
            status=status,
            configurable=configurable,
            reason="accounting_disabled",
        )
        response["duration_ms"] = duration_ms
        return response
    try:
        store = SQLiteTraceStore(configurable.trace_store_path)
        if configurable.token_usage_accounting_enabled:
            # Runs whose transitions predate the transition-time backfill are
            # healed from their durable Operation Journal on first read.
            reconcile_run_gateway_usage(
                run_id,
                runs_dir=configurable.runs_dir,
                config={"configurable": configurable.model_dump(mode="json")},
            )
        reserved = _outstanding_usage_budget(configurable, run_id)
        response = store.get_usage_accounting(
            run_id,
            reserved_budget=reserved,
        )
    except (OSError, RuntimeError, sqlite3.Error):
        logger.warning(
            "Token accounting storage unavailable for run %s",
            run_id,
            exc_info=True,
        )
        response = _unavailable_usage_response(
            run_id,
            status=status,
            configurable=configurable,
            reason="storage_unavailable",
        )
    if response["status"] == "unknown" or (
        status in {"completed", "failed", "cancelled", "interrupted"}
        and response["status"]
        not in {"completed", "failed", "cancelled", "interrupted", "unknown"}
    ):
        # The run-level status is authoritative once terminal; a trace row
        # left "running" by an unfinished path must not leak to clients.
        response["status"] = status
    if not response.get("duration_ms"):
        response["duration_ms"] = duration_ms
    return response


def _apply_gateway_cost(
    response: dict[str, Any],
    *,
    spend_micro_usd: int,
    source: str,
    updated_at: float | None,
) -> dict[str, Any]:
    """Overlay LiteLLM's cumulative spend onto the local logical-usage projection."""
    totals = response.setdefault("totals", {})
    totals["cost"] = {
        "estimated_cost_micro_usd": spend_micro_usd,
        "cost_source": source,
        "price_table_hash": None,
    }
    budgets = totals.setdefault("budgets", {})
    cost_budget = budgets.setdefault("cost_micro_usd", {})
    cost_budget.update(settled=spend_micro_usd, estimated=0, reserved=0)
    response["cost_source"] = source
    if updated_at is not None:
        response["updated_at"] = updated_at
    return response


async def _attach_gateway_stage_breakdown(
    response: dict[str, Any],
    run_id: str,
    *,
    settings: RunKeySettings,
) -> None:
    """Overlay gateway-side per-stage spend as an authoritative comparison.

    Rows are matched by the ``run:{id}`` request tag (the gateway stores key
    hashes, so key-based filtering is unreliable) and bounded to recent
    history; every failure path just skips the overlay and the local by-stage
    accounting remains the base view.
    """
    client = LiteLLMSpendClient(settings)
    try:
        logs = await client.run_spend_logs(run_id)
    except (httpx.HTTPError, ValueError, RuntimeError):
        return
    finally:
        await client.aclose()
    if not logs:
        return
    breakdowns = response.setdefault("breakdowns", {})
    breakdowns["by_stage_gateway"] = [
        {
            "stage": bucket.key,
            "calls": bucket.calls,
            "total_tokens": bucket.total_tokens,
            "spend_micro_usd": bucket.spend_micro_usd,
        }
        for bucket in aggregate_spend_logs_by_tag_prefix(logs, "stage:")
    ]


async def _reconcile_litellm_usage(
    run_id: str,
    response: dict[str, Any],
    *,
    configurable: Configuration,
    manifest: Any,
) -> dict[str, Any]:
    if configurable.model_backend != "litellm":
        return response
    # The stage breakdown matches spend-log request tags instead of the run
    # key, so it stays available for terminal runs whose key was finalized.
    try:
        await _attach_gateway_stage_breakdown(
            response,
            run_id,
            settings=RunKeySettings.from_env(),
        )
    except LiteLLMKeyConfigurationError:
        pass
    admin: LiteLLMKeyAdminClient | None = None
    try:
        settings = RunKeySettings.from_env()
        store = RunKeySecretStore(configurable.runs_dir, settings.encryption_key)
        admin = LiteLLMKeyAdminClient(settings)
        manager = RunKeyManager(settings, store, admin)
        spend = await manager.authoritative_spend_micro_usd(run_id)
        updated_at = time.time()
        context = RunContextStore(run_id, runs_dir=configurable.runs_dir)
        context.update_reconciliation_fields(
            litellm_spend_micro_usd=spend,
            litellm_spend_updated_at=updated_at,
        )
        return _apply_gateway_cost(
            response,
            spend_micro_usd=spend,
            source="litellm_gateway",
            updated_at=updated_at,
        )
    except Exception:  # noqa: BLE001 - return explicit stale state
        snapshot = getattr(manifest, "litellm_spend_micro_usd", None)
        updated_at = getattr(manifest, "litellm_spend_updated_at", None)
        if snapshot is not None:
            return _apply_gateway_cost(
                response,
                spend_micro_usd=int(snapshot),
                source="stale_gateway_snapshot",
                updated_at=updated_at,
            )
        # No gateway snapshot exists yet; keep the local accounting labels so
        # callers see an honest local estimate instead of a bogus "stale" flag.
        return response
    finally:
        if admin is not None:
            await admin.aclose()


class RunUsageRoutes:
    """Bind ownership and the current native host without importing server."""

    def __init__(self, *, native_service, require_run_owner):
        self.native_service = native_service
        self.require_run_owner = require_run_owner
        self.router = APIRouter()
        self.router.add_api_route("/runs/{run_id}/usage", self.get_run_usage_accounting, methods=["GET"])

    async def get_run_usage_accounting(
        self,
        run_id: str,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> dict[str, Any]:
        """Return content-free token accounting for one owned research run."""
        native_service = self.native_service()
        if native_service is not None:
            from open_deep_research.agentscope_runtime.usage_projection import project_usage
            try:
                return await project_usage(native_service.store, run_id, user.user_id,
                    _unavailable_usage_response(run_id, configurable=Configuration.from_runnable_config(None)))
            except KeyError:
                from open_deep_research.agentscope_runtime.recovery_store import RecoveryConflict
                try:
                    archive = native_service.history(run_id, user.user_id)
                    configurable = Configuration.from_runnable_config(None)
                    return await asyncio.to_thread(
                        archive.usage,
                        configurable.trace_store_path,
                        _unavailable_usage_response(run_id, configurable=configurable),
                    )
                except KeyError:
                    raise HTTPException(404, "Run not found") from None
                except (RecoveryConflict, ValueError):
                    raise HTTPException(409, "historical_artifact_corrupted") from None
        record, configurable = self.require_run_owner(run_id, user)
        manifest = None
        with contextlib.suppress(ValueError, JournalCorruptedError, OSError):
            manifest = RunContextStore(
                run_id,
                runs_dir=configurable.runs_dir,
            ).load_manifest()
        manifest_status = manifest.status if manifest is not None else None
        if record is None:
            status = manifest_status or "unknown"
        elif (
            manifest_status in {"completed", "failed", "cancelled", "interrupted"}
            and record.status != manifest_status
        ):
            # The durable manifest is authoritative once terminal; an in-memory
            # record can lag behind a just-finished run.
            status = manifest_status
        else:
            status = record.status
        duration_ms = (
            max(0, int((manifest.updated_at - manifest.created_at) * 1000))
            if manifest is not None and status in {
                "completed", "failed", "cancelled", "interrupted",
            }
            else None
        )
        response = await asyncio.to_thread(
            _load_run_usage_response,
            run_id,
            status=status,
            configurable=configurable,
            duration_ms=duration_ms,
        )
        return await _reconcile_litellm_usage(
            run_id,
            response,
            configurable=configurable,
            manifest=manifest,
        )


