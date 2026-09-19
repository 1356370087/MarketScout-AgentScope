"""LiteLLM spend-log and key-spend analytics (owner-scoped aggregation).

All figures read here are per-request gateway spend records written by the
LiteLLM proxy into PostgreSQL. The application treats them as authoritative
for cost while keeping local trace accounting for tokens and operations.

Parsing is defensive by design: LiteLLM list/log payload shapes drift across
proxy versions, so unknown or missing fields degrade to neutral values
instead of failing the analytics endpoints.
"""

from __future__ import annotations

import math
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from open_deep_research.models.credentials import RunKeySettings


@dataclass(frozen=True, slots=True)
class GatewayKeySpend:
    """One Virtual Key's cumulative spend as reported by the gateway."""

    key_alias: str
    spend_micro_usd: int
    max_budget_usd: float | None
    models: tuple[str, ...]

    @property
    def run_id(self) -> str | None:
        """Extract the owning run from the ``run-{run_id}-{hex4}`` alias."""
        if not self.key_alias.startswith("run-"):
            return None
        remainder = self.key_alias[len("run-") :]
        head, _, tail = remainder.rpartition("-")
        if not head or len(tail) != 8:
            return None
        return head


@dataclass(frozen=True, slots=True)
class SpendAggregate:
    """Aggregated gateway spend bucketed by a request-tag dimension."""

    key: str
    calls: int
    total_tokens: int
    spend_micro_usd: int


def usd_to_micro_usd(value: Any) -> int:
    """Convert a gateway-reported USD figure to integral micro-USD."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return 0
    if not amount.is_finite():
        return 0
    return int(amount * 1_000_000)


def _alias_of(entry: Mapping[str, Any]) -> str:
    return str(entry.get("key_alias") or entry.get("alias") or "")


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_key_spend_entries(entries: list[Mapping[str, Any]]) -> list[GatewayKeySpend]:
    """Project raw ``/key/list`` rows into typed, alias-identified records."""
    projected: list[GatewayKeySpend] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        alias = _alias_of(entry)
        if not alias:
            continue
        raw_models = entry.get("models")
        models = (
            tuple(str(item) for item in raw_models if isinstance(item, str))
            if isinstance(raw_models, list)
            else ()
        )
        projected.append(
            GatewayKeySpend(
                key_alias=alias,
                spend_micro_usd=usd_to_micro_usd(entry.get("spend") or 0),
                max_budget_usd=_float_or_none(entry.get("max_budget")),
                models=models,
            )
        )
    return projected


def run_spend_index(entries: list[GatewayKeySpend]) -> dict[str, int]:
    """Sum per-key spend by owning run; re-issued keys of one run add up."""
    index: dict[str, int] = {}
    for entry in entries:
        run_id = entry.run_id
        if run_id is None:
            continue
        index[run_id] = index.get(run_id, 0) + entry.spend_micro_usd
    return index


def _tags_of(log: Mapping[str, Any]) -> list[str]:
    tags = log.get("request_tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except ValueError:
            return []
    if isinstance(tags, list):
        return [str(tag) for tag in tags if isinstance(tag, str)]
    return []


def aggregate_spend_logs_by_tag_prefix(
    logs: list[Mapping[str, Any]],
    prefix: str,
) -> list[SpendAggregate]:
    """Aggregate spend logs by a ``prefix:value`` request-tag dimension.

    Logs without the tag are skipped so unrelated maintenance traffic (e.g.
    the egress classifier on a service key) never pollutes run breakdowns.
    """
    buckets: dict[str, SpendAggregate] = {}
    for log in logs:
        matched = [tag[len(prefix):] for tag in _tags_of(log) if tag.startswith(prefix)]
        if not matched:
            continue
        tokens = 0
        raw_tokens = log.get("total_tokens")
        if isinstance(raw_tokens, int | float) and math.isfinite(raw_tokens):
            tokens = int(raw_tokens)
        spend = usd_to_micro_usd(log.get("spend") or 0)
        for key in matched:
            bucket = buckets.get(key)
            buckets[key] = SpendAggregate(
                key=key,
                calls=(bucket.calls if bucket else 0) + 1,
                total_tokens=(bucket.total_tokens if bucket else 0) + tokens,
                spend_micro_usd=(bucket.spend_micro_usd if bucket else 0) + spend,
            )
    return [buckets[key] for key in sorted(buckets)]


class LiteLLMSpendClient:
    """Read-only spend analytics over the LiteLLM admin API."""

    def __init__(
        self,
        settings: RunKeySettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create a spend client sharing the admin connection conventions."""
        self.settings = settings
        management_url = settings.base_url.rstrip("/")
        if management_url.endswith("/v1"):
            management_url = management_url[:-3]
        self._client = client or httpx.AsyncClient(
            base_url=management_url,
            headers={"Authorization": f"Bearer {settings.master_key}"},
            timeout=30,
        )
        self._owns_client = client is None

    async def list_keys_with_spend(
        self,
        *,
        page_size: int = 100,
        max_pages: int = 20,
    ) -> list[GatewayKeySpend]:
        """Page through ``/key/list`` and project typed spend records.

        ``return_full_object`` is required: LiteLLM 1.98 returns bare key
        hashes otherwise, and those carry neither alias nor spend.
        """
        entries: list[Mapping[str, Any]] = []
        page = 1
        while page <= max_pages:
            response = await self._client.get(
                "/key/list",
                params={
                    "page": page,
                    "size": page_size,
                    "return_full_object": "true",
                },
            )
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("keys") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                break
            entries.extend(item for item in batch if isinstance(item, Mapping))
            if len(batch) < page_size:
                break
            page += 1
        return parse_key_spend_entries(entries)

    async def spend_logs(
        self,
        *,
        api_key: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        size: int = 100,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Read ``/spend/logs`` rows; callers aggregate defensively.

        1.98 returns a bare JSON array; older versions wrapped rows in
        ``{"data": [...]}`` — both shapes are accepted. ``api_key`` matching
        is unreliable (the gateway stores key hashes), so run-scoped callers
        should filter client-side on the ``run:{id}`` request tag instead.
        """
        params: dict[str, Any] = {"page": page, "size": size}
        if api_key:
            params["api_key"] = api_key
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        rows: list[dict[str, Any]] = []
        current = page
        while current < page + max_pages:
            response = await self._client.get("/spend/logs", params=params)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                data = payload
            else:
                data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                break
            rows.extend(item for item in data if isinstance(item, dict))
            if len(data) < size:
                break
            params["page"] = current = current + 1
        return rows

    async def run_spend_logs(
        self,
        run_id: str,
        *,
        size: int = 500,
        max_pages: int = 4,
    ) -> list[dict[str, Any]]:
        """Recent spend logs belonging to one run, matched by request tag."""
        tag = f"run:{run_id}"
        logs = await self.spend_logs(size=size, max_pages=max_pages)
        return [
            log
            for log in logs
            if tag in _tags_of(log)
        ]

    async def aclose(self) -> None:
        """Close an internally owned HTTP pool."""
        if self._owns_client:
            await self._client.aclose()


__all__ = [
    "GatewayKeySpend",
    "LiteLLMSpendClient",
    "SpendAggregate",
    "aggregate_spend_logs_by_tag_prefix",
    "parse_key_spend_entries",
    "run_spend_index",
    "usd_to_micro_usd",
]
