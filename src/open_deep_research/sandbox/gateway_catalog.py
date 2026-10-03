"""Worker-side schema-only projections of dynamic Gateway tools."""

# Delegating properties intentionally mirror the structural Tool protocol.
# ruff: noqa: D102

from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from open_deep_research.config_types import RuntimeConfig
from open_deep_research.sandbox.wire import (
    GatewayCatalogToolV1,
    GatewayToolCatalogOutcomeV1,
    GatewayToolCatalogRequestV1,
)
from open_deep_research.tools.base import (
    ProgressCallback,
    ToolContext,
    ToolEffect,
    ToolExecutionZone,
    ToolOrigin,
    ToolResult,
)


class GatewayCatalogInput(BaseModel):
    """Accept catalog arguments locally; Gateway performs exact validation."""

    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True, slots=True)
class GatewayCatalogTool:
    """A model-facing schema with no physical implementation in the Worker."""

    catalog: GatewayCatalogToolV1

    @property
    def name(self) -> str:
        return self.catalog.name

    @property
    def input_schema(self) -> type[GatewayCatalogInput]:
        return GatewayCatalogInput

    @property
    def origin(self) -> ToolOrigin:
        return ToolOrigin(self.catalog.origin)

    @property
    def effect(self) -> ToolEffect:
        return ToolEffect(self.catalog.effect)

    @property
    def execution_zone(self) -> ToolExecutionZone:
        return ToolExecutionZone.GATEWAY

    @property
    def retryable(self) -> bool:
        return self.catalog.retryable

    @property
    def concurrency_safe(self) -> bool:
        return self.catalog.concurrency_safe

    @property
    def supports_idempotency(self) -> bool:
        return False

    @property
    def max_output_chars(self) -> int | None:
        return self.catalog.max_output_chars

    @property
    def model_definition(self) -> dict[str, Any]:
        return dict(self.catalog.definition)

    async def description(self, input: BaseModel | None = None) -> str:
        del input
        return str(self.catalog.definition.get("description") or "")

    def prompt(self, config: RuntimeConfig) -> str | None:
        del config
        return self.catalog.prompt

    def is_enabled(self, config: RuntimeConfig) -> bool:
        del config
        return True

    def egress_urls(self, input: dict[str, Any]) -> list[str]:
        del input
        return []

    async def call(
        self,
        input: GatewayCatalogInput,
        context: ToolContext,
        on_progress: ProgressCallback | None = None,
    ) -> ToolResult[Any]:
        del input, context, on_progress
        raise RuntimeError("sandbox_gateway_catalog_tool_requires_proxy")


async def load_gateway_catalog_tools(
    role: str,
    config: RuntimeConfig,
    existing_names: set[str],
    *, gateway_url: str | None = None, task_token: SecretStr | None = None,
) -> list[GatewayCatalogTool]:
    """Fetch replay-protected dynamic schemas from the authoritative Gateway."""
    gateway_url = gateway_url if gateway_url is not None else os.getenv("SANDBOX_GATEWAY_URL", "")
    task_token = task_token.get_secret_value() if task_token is not None else os.getenv("SANDBOX_TASK_TOKEN", "")
    if not gateway_url or not task_token:
        return []
    metadata = config.get("metadata", {})
    request = GatewayToolCatalogRequestV1(
        run_id=str(metadata.get("run_id") or "default"),
        task_id=str(metadata.get("task_id") or "researcher"),
        role=role,
        stage="researching",
    )
    headers = {
        "Authorization": f"Bearer {task_token}",
        "Content-Type": "application/json",
        "X-Sandbox-Timestamp": str(time.time()),
        "X-Sandbox-Nonce": secrets.token_urlsafe(24),
    }
    async with httpx.AsyncClient(base_url=gateway_url, timeout=60) as client:
        while True:
            # Discovery may itself need a human network grant. Keep it pending
            # before binding the Agent, with fresh replay headers on each RPC.
            headers["X-Sandbox-Timestamp"] = str(time.time())
            headers["X-Sandbox-Nonce"] = secrets.token_urlsafe(24)
            response = await client.post(
                "/v1/tools/catalog",
                content=request.model_dump_json(),
                headers=headers,
            )
            response.raise_for_status()
            outcome = GatewayToolCatalogOutcomeV1.model_validate(response.json())
            if outcome.status != "approval_required":
                break
            await asyncio.sleep(0.25)
    return [
        GatewayCatalogTool(item)
        for item in outcome.tools
        if item.name not in existing_names
    ]
