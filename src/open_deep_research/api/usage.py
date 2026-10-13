"""Owner-scoped usage analytics application service and HTTP route."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from open_deep_research.configuration import Configuration
from open_deep_research.models.credentials import (
    RunKeySettings,
    LiteLLMKeyConfigurationError,
)
from open_deep_research.models.spend import LiteLLMSpendClient, run_spend_index
from open_deep_research.observability import SQLiteTraceStore
from open_deep_research.run_context import RunContextStore, JournalCorruptedError
from security.rbac import Principal, require_permissions
from security.rbac.permissions import RESEARCH_RUN_READ_OWN

logger = logging.getLogger(__name__)
router = APIRouter(tags=["usage"])


def _analytics_cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
    except ValueError, UnicodeDecodeError, base64.binascii.Error:
        raise HTTPException(status_code=400, detail="invalid_cursor") from None


def _sum_accounting_vectors(
    reports: list[dict[str, Any]], track: Literal["reported", "estimated"]
) -> dict[str, int]:
    keys = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_tokens",
    )
    return {
        key: sum(int(report["totals"][track].get(key) or 0) for report in reports)
        for key in keys
    }


def _normalize_analytics_status(status: str | None) -> str | None:
    aliases = {
        "completed": "success",
        "failed": "error",
    }
    normalized = (status or "").strip().lower()
    return aliases.get(normalized, normalized) or None


def _usage_manifest_title(
    run_id: str,
    *,
    configurable: Configuration,
    fallback: str,
) -> str:
    """Read a display title from the content store, never the usage database."""
    try:
        manifest = RunContextStore(
            run_id,
            runs_dir=configurable.runs_dir,
        ).load_manifest()
    except ValueError, JournalCorruptedError, OSError:
        return fallback
    return str(manifest.title or fallback)


def _build_usage_analytics_response(
    *,
    range_name: Literal["7d", "30d", "retained"],
    status: str | None,
    provider: str | None,
    model: str | None,
    query: str | None,
    timezone_name: str,
    timezone_info: ZoneInfo,
    limit: int,
    offset: int,
    user_id: str,
    gateway_spend_by_run: dict[str, int] | None = None,
    native_reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build historical usage off the event loop using owner-filtered SQL.

    ``gateway_spend_by_run`` carries LiteLLM's authoritative per-run spend.
    ``None`` means the gateway was unreachable; an empty dict means reachable
    with nothing attributable. Either way the local projection stays intact.
    """
    configurable = Configuration.from_runnable_config(None)
    retention_days = float(
        configurable.run_retention_days
        if configurable.trace_retention_days is None
        else configurable.trace_retention_days
    )
    unlimited_retention = retention_days <= 0
    now = time.time()
    if range_name == "retained":
        actual_days = 0.0 if unlimited_retention else retention_days
        cutoff = None if unlimited_retention else now - actual_days * 86400
    else:
        requested_days = 7.0 if range_name == "7d" else 30.0
        actual_days = (
            requested_days
            if unlimited_retention
            else min(requested_days, retention_days)
        )
        cutoff = now - max(0.0, actual_days) * 86400

    store = SQLiteTraceStore(configurable.trace_store_path)
    native_by_run = {report["run_id"]: report for report in native_reports or []}
    owned_runs = [run for run in store.list_runs_for_usage(
        user_id=user_id,
        cutoff=cutoff,
        status=_normalize_analytics_status(status),
    ) if run["run_id"] not in native_by_run]
    for report in native_by_run.values():
        native_status = _normalize_analytics_status(report["status"])
        if cutoff is not None and (report["started_at"] or 0) < cutoff:
            continue
        if status and native_status != _normalize_analytics_status(status):
            continue
        owned_runs.append({**report, "status": native_status,
                           "metadata_json": json.dumps({"title": report["title"]})})
    owned_runs.sort(key=lambda run: (run["started_at"] or 0, run["run_id"]), reverse=True)
    reports: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    selected_runs: list[tuple[dict[str, Any], str]] = []
    normalized_query = (query or "").strip().lower()
    for run in owned_runs:
        title = str(run["run_id"])
        try:
            metadata = json.loads(run.get("metadata_json") or "{}")
        except TypeError, json.JSONDecodeError:
            metadata = {}
        title = str(metadata.get("title") or title)
        if normalized_query:
            if normalized_query not in f"{title} {run['run_id']}".lower():
                title = _usage_manifest_title(
                    str(run["run_id"]),
                    configurable=configurable,
                    fallback=title,
                )
            if normalized_query not in f"{title} {run['run_id']}".lower():
                continue
        selected_runs.append((run, title))

    reports_by_run = store.get_usage_accounting_many(
        [str(run["run_id"]) for run, _title in selected_runs if run["run_id"] not in native_by_run],
        provider=provider,
        model=model,
    )
    reports_by_run.update(native_by_run)
    for run, title in selected_runs:
        report = reports_by_run[str(run["run_id"])]
        if (provider or model) and not report["totals"]["calls"]["attempts"]:
            continue
        reports.append(report)
        run_rows.append(
            {
                "run_id": run["run_id"],
                "title": title,
                "status": run.get("status"),
                "started_at": run.get("started_at"),
                "ended_at": run.get("ended_at"),
                "duration_ms": run.get("duration_ms"),
                "accounting_status": report["accounting_status"],
                "reported": report["totals"]["reported"],
                "estimated": report["totals"]["estimated"],
                "calls": report["totals"]["calls"],
                "cost": report["totals"]["cost"],
                "operations": report["operations"],
            }
        )

    # Gateway spend is authoritative where attributable; owner scoping comes
    # for free because only rows already filtered to this user are overlaid.
    gateway_attributed_runs = 0
    gateway_spend_total = 0
    if gateway_spend_by_run is not None and not (provider or model):
        for row in run_rows:
            spend = gateway_spend_by_run.get(str(row["run_id"]))
            if spend is None:
                continue
            row["cost"] = {
                "estimated_cost_micro_usd": spend,
                "cost_source": "litellm_gateway",
                "price_table_hash": None,
            }
            gateway_attributed_runs += 1
            gateway_spend_total += int(spend)

    if unlimited_retention and range_name == "retained" and run_rows:
        oldest = min(float(row["started_at"] or now) for row in run_rows)
        actual_days = max(0.0, (now - oldest) / 86400)

    distributions: dict[str, dict[str, dict[str, Any]]] = {
        "provider": {},
        "model": {},
        "status": {},
    }
    daily: dict[str, dict[str, Any]] = {}
    for report, row in zip(reports, run_rows, strict=True):
        date_key = (
            datetime.fromtimestamp(
                float(row["started_at"]),
                timezone_info,
            )
            .date()
            .isoformat()
        )
        day = daily.setdefault(
            date_key,
            {
                "date": date_key,
                "reported_tokens": 0,
                "estimated_tokens": 0,
                "run_count": 0,
                "provider_reported_responses": 0,
                "attempts": 0,
                "rate_429_sum": 0.0,
                "throughput_sum": 0.0,
                "cache_hit_rate_sum": 0.0,
            },
        )
        day["reported_tokens"] += int(row["reported"]["total_tokens"])
        day["estimated_tokens"] += int(row["estimated"]["total_tokens"])
        day["run_count"] += 1
        day["provider_reported_responses"] += int(row["calls"]["provider_reported"])
        day["attempts"] += int(row["calls"]["attempts"])
        day["rate_429_sum"] += float(row["operations"]["rate_429"])
        day["throughput_sum"] += float(row["operations"]["output_tokens_per_second"])
        day["cache_hit_rate_sum"] += float(row["operations"]["cache_hit_rate"] or 0)
        status_key = str(row["status"] or "unknown")
        status_bucket = distributions["status"].setdefault(
            status_key,
            {
                "key": status_key,
                "reported_tokens": 0,
                "estimated_tokens": 0,
                "run_count": 0,
            },
        )
        status_bucket["reported_tokens"] += int(row["reported"]["total_tokens"])
        status_bucket["estimated_tokens"] += int(row["estimated"]["total_tokens"])
        status_bucket["run_count"] += 1
        for bucket in report["breakdowns"]["by_model"]:
            full_key = str(bucket["key"])
            provider_key, _, model_key = full_key.partition(":")
            if not model_key:
                model_key = provider_key
                provider_key = "unknown"
            dimensions = [("model", full_key)] if "by_provider" in report["breakdowns"] else [("provider", provider_key), ("model", model_key)]
            for dimension, key in dimensions:
                target = distributions[dimension].setdefault(
                    key,
                    {
                        "key": key,
                        "reported_tokens": 0,
                        "estimated_tokens": 0,
                        "call_count": 0,
                    },
                )
                target["reported_tokens"] += int(bucket["reported"]["total_tokens"])
                target["estimated_tokens"] += int(bucket["estimated"]["total_tokens"])
                target["call_count"] += int(bucket["call_count"])
        for bucket in report["breakdowns"].get("by_provider", []):
            target = distributions["provider"].setdefault(bucket["key"], {
                "key": bucket["key"], "reported_tokens": 0, "estimated_tokens": 0, "call_count": 0})
            target["reported_tokens"] += bucket["reported"]["total_tokens"]
            target["estimated_tokens"] += bucket["estimated"]["total_tokens"]
            target["call_count"] += bucket["call_count"]

    daily_rows: list[dict[str, Any]] = []
    for day in sorted(daily.values(), key=lambda item: item["date"]):
        count = max(1, int(day["run_count"]))
        attempts = int(day.pop("attempts"))
        provider_reported = int(day.pop("provider_reported_responses"))
        day["coverage_ratio"] = provider_reported / attempts if attempts else 0.0
        day["rate_429"] = float(day.pop("rate_429_sum")) / count
        day["output_tokens_per_second"] = float(day.pop("throughput_sum")) / count
        day["cache_hit_rate"] = float(day.pop("cache_hit_rate_sum")) / count
        daily_rows.append(day)

    page = run_rows[offset : offset + limit]
    for item in page:
        if item["run_id"] in native_by_run:
            continue
        item["title"] = _usage_manifest_title(
            str(item["run_id"]),
            configurable=configurable,
            fallback=str(item["title"]),
        )
    next_offset = offset + len(page)
    next_cursor = (
        base64.urlsafe_b64encode(str(next_offset).encode()).decode()
        if next_offset < len(run_rows)
        else None
    )
    costs = [
        row["cost"]["estimated_cost_micro_usd"]
        for row in run_rows
        if row["cost"]["estimated_cost_micro_usd"] is not None
    ]
    attempts = sum(int(row["calls"]["attempts"]) for row in run_rows)
    provider_reported = sum(int(row["calls"]["provider_reported"]) for row in run_rows)
    return {
        "schema_version": 1,
        "range": range_name,
        "timezone": timezone_name,
        "retention_days": retention_days,
        "actual_range_days": actual_days,
        "summary": {
            "run_count": len(run_rows),
            "reported": _sum_accounting_vectors(reports, "reported"),
            "estimated": _sum_accounting_vectors(reports, "estimated"),
            "estimated_cost_micro_usd": (
                sum(int(value) for value in costs) if costs else None
            ),
            "coverage_ratio": (provider_reported / attempts if attempts else 0.0),
            "gateway_spend_micro_usd": (
                gateway_spend_total if gateway_spend_by_run is not None else None
            ),
            "gateway_attributed_runs": (
                gateway_attributed_runs if gateway_spend_by_run is not None else None
            ),
            "gateway_status": (
                "ok" if gateway_spend_by_run is not None else "unavailable"
            ),
        },
        "daily": daily_rows,
        "distributions": {
            key: list(value.values()) for key, value in distributions.items()
        },
        "runs": page,
        "next_cursor": next_cursor,
    }


async def _fetch_gateway_run_spend() -> dict[str, int] | None:
    """Load authoritative per-run spend from LiteLLM; ``None`` when offline.

    Fail-open by contract: analytics must keep working with purely local
    accounting whenever the gateway or its control-plane env is unavailable.
    """
    if Configuration.from_runnable_config(None).model_backend != "litellm":
        return None
    try:
        settings = RunKeySettings.from_env()
    except LiteLLMKeyConfigurationError:
        return None
    client = LiteLLMSpendClient(settings)
    try:
        entries = await client.list_keys_with_spend()
    except httpx.HTTPError, ValueError, RuntimeError:
        logger.warning("LiteLLM key spend list unavailable", exc_info=True)
        return None
    finally:
        await client.aclose()
    return run_spend_index(entries)


@router.get("/usage/analytics")
async def get_usage_analytics(
    request: Request,
    range: Literal["7d", "30d", "retained"] = "30d",  # noqa: A002
    status: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    query: str | None = None,
    timezone: str = "Asia/Shanghai",
    limit: int = 50,
    cursor: str | None = None,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> dict[str, Any]:
    """Aggregate retained token usage for the current run owner only."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    try:
        timezone_info = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise HTTPException(status_code=422, detail="invalid timezone") from None
    offset = _analytics_cursor_offset(cursor)
    gateway_spend_by_run = await _fetch_gateway_run_spend()
    service = getattr(request.app.state, "native_research_service", None)
    native_reports = await _native_usage_reports(service.store, user.user_id, provider=provider, model=model) if service is not None else []
    return await asyncio.to_thread(
        _build_usage_analytics_response,
        range_name=range,
        status=status,
        provider=provider,
        model=model,
        query=query,
        timezone_name=timezone,
        timezone_info=timezone_info,
        limit=limit,
        offset=offset,
        user_id=user.user_id,
        gateway_spend_by_run=gateway_spend_by_run,
        native_reports=native_reports,
    )


async def _native_usage_reports(store, owner, *, provider=None, model=None):
    """Use the same fenced receipts as run details, scoped to the current owner."""
    from sqlalchemy import select
    from open_deep_research.agentscope_runtime.usage_projection import project_usage

    async with store.engine.connect() as connection:
        identifiers = (await connection.execute(select(store.runs.c.run_id).where(
            store.runs.c.user_id == owner))).scalars().all()
    reports = []
    for run_id in identifiers:
        try:
            reports.append(await project_usage(store, run_id, owner,
                {"totals": SQLiteTraceStore._empty_accounting_totals(), "operations": {}},
                provider=provider, model=model))
        except KeyError:
            # The owner may delete a run between enumeration and projection.
            continue
    return reports
