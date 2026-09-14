"""LiteLLM model catalog loading, validation and frozen Run projections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


class ModelCatalogEntry(BaseModel):
    """Pricing and token capabilities required by application recovery logic."""

    model_config = ConfigDict(extra="ignore")

    model_name: str
    base_model: str | None = None
    context_window: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    input_cost_per_token: float = Field(ge=0)
    output_cost_per_token: float = Field(ge=0)


class ModelCatalogError(RuntimeError):
    """Raised when LiteLLM cannot provide a safe model capability snapshot."""


def _positive_int(*values: Any) -> int | None:
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _nonnegative_float(*values: Any) -> float | None:
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def parse_model_info(payload: Mapping[str, Any]) -> dict[str, ModelCatalogEntry]:
    """Parse LiteLLM `/model/info` responses across supported OSS shapes."""
    raw_items = payload.get("data") or payload.get("models") or payload.get("model_list") or []
    if not isinstance(raw_items, list):
        raise ModelCatalogError("litellm_model_info_invalid_shape")
    grouped: dict[str, list[ModelCatalogEntry]] = {}
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        info = raw.get("model_info") if isinstance(raw.get("model_info"), Mapping) else {}
        params = raw.get("litellm_params") if isinstance(raw.get("litellm_params"), Mapping) else {}
        name = str(raw.get("model_name") or info.get("model_name") or "").strip()
        if not name:
            continue
        context_window = _positive_int(
            info.get("max_input_tokens"),
            info.get("max_tokens"),
            raw.get("max_tokens"),
        )
        max_output = _positive_int(
            info.get("max_output_tokens"),
            params.get("max_tokens"),
            context_window,
        )
        input_cost = _nonnegative_float(
            info.get("input_cost_per_token"),
            params.get("input_cost_per_token"),
        )
        output_cost = _nonnegative_float(
            info.get("output_cost_per_token"),
            params.get("output_cost_per_token"),
        )
        if None in {context_window, max_output, input_cost, output_cost}:
            continue
        grouped.setdefault(name, []).append(
            ModelCatalogEntry(
                model_name=name,
                base_model=str(info.get("base_model") or "") or None,
                context_window=context_window,
                max_output_tokens=max_output,
                input_cost_per_token=input_cost,
                output_cost_per_token=output_cost,
            )
        )
    result: dict[str, ModelCatalogEntry] = {}
    for name, deployments in grouped.items():
        result[name] = ModelCatalogEntry(
            model_name=name,
            base_model=deployments[0].base_model,
            context_window=min(item.context_window for item in deployments),
            max_output_tokens=min(item.max_output_tokens for item in deployments),
            input_cost_per_token=max(item.input_cost_per_token for item in deployments),
            output_cost_per_token=max(item.output_cost_per_token for item in deployments),
        )
    return result


def validate_model_catalog(
    catalog: Mapping[str, ModelCatalogEntry],
    required_models: Iterable[str],
    *,
    budget_enabled: bool,
) -> None:
    """Fail startup/run creation when required limits or prices are unknown."""
    missing = sorted({model for model in required_models if model not in catalog})
    if missing:
        raise ModelCatalogError("litellm_model_info_missing:" + ",".join(missing))
    if budget_enabled:
        unpriced = sorted(
            model
            for model in set(required_models)
            if catalog[model].input_cost_per_token <= 0
            or catalog[model].output_cost_per_token <= 0
        )
        if unpriced:
            raise ModelCatalogError("litellm_model_price_unknown:" + ",".join(unpriced))


class LiteLLMModelCatalogClient:
    """Read the Proxy model catalog with a restricted control-plane credential."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create a reusable model-info client."""
        management_url = base_url.rstrip("/")
        if management_url.endswith("/v1"):
            management_url = management_url[:-3]
        self._client = client or httpx.AsyncClient(
            base_url=management_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        self._owns_client = client is None

    async def load(self) -> dict[str, ModelCatalogEntry]:
        """Fetch and parse the authoritative LiteLLM model catalog."""
        response = await self._client.get("/model/info")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ModelCatalogError("litellm_model_info_invalid_shape")
        return parse_model_info(payload)

    async def aclose(self) -> None:
        """Close an internally owned HTTP connection pool."""
        if self._owns_client:
            await self._client.aclose()


def freeze_catalog_snapshot(
    catalog: Mapping[str, ModelCatalogEntry],
    required_models: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Project only Run-authorized model groups into its immutable Manifest config."""
    return {
        model: catalog[model].model_dump(mode="json")
        for model in sorted(set(required_models))
    }


__all__ = [
    "LiteLLMModelCatalogClient",
    "ModelCatalogEntry",
    "ModelCatalogError",
    "freeze_catalog_snapshot",
    "parse_model_info",
    "validate_model_catalog",
]
