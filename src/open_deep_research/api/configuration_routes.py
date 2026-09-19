"""Frontend capability schema and bounded gateway model catalog."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
import httpx
from fastapi import APIRouter, Depends
from open_deep_research.api.activity_routes import _task_activity_preview_allowed
from open_deep_research.configuration import Configuration
from open_deep_research.documents.database import document_health
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.events.public import PUBLIC_EVENT_SCHEMA_VERSION
from open_deep_research.events.publications import PUBLICATION_EVENT_SCHEMA_VERSION
from open_deep_research.events.task_activity import PUBLIC_TASK_ACTIVITY_SCHEMA_VERSION
from open_deep_research.models.catalog import (
    LiteLLMModelCatalogClient,
    ModelCatalogError,
)
from open_deep_research.models.credentials import (
    RunKeySettings,
    LiteLLMKeyConfigurationError,
)
from open_deep_research.report.models import PublisherTheme
from open_deep_research.report.publication_store import (
    PUBLICATION_SCHEMA_VERSION,
    get_publisher_settings,
    worker_available,
)
from open_deep_research.report.publishers import PUBLICATION_FORMATS
from security.rbac import Principal, require_active_user

router = APIRouter(tags=["configuration"])


FRONTEND_EDITABLE_CONFIG_KEYS = (
    "allow_clarification",
    "enable_async_research",
    "enable_human_in_loop",
    "summarization_model",
    "summarization_model_max_tokens",
    "research_model",
    "research_model_max_tokens",
    "compression_model",
    "compression_model_max_tokens",
    "final_report_model",
    "final_report_model_max_tokens",
    "search_api",
    "web_pipeline_mode",
    "web_pipeline_shadow_sample_rate",
    "web_min_source_authority",
    "search_candidate_limit",
    "max_fetches_per_researcher",
    "max_concurrent_research_units",
    "max_researcher_iterations",
    "max_react_tool_calls",
    "hitl_require_plan_approval",
    "hitl_require_outline_approval",
    "hitl_max_plan_revisions",
    "hitl_feedback_mode",
    "report_type",
    "output_format",
    "quality_evaluation_enabled",
    "quality_evaluation_model",
    "quality_evaluation_model_max_tokens",
    "quality_evaluation_rigor",
    "quality_evaluation_min_sources",
    "quality_evaluation_max_input_chars",
    "quality_risk_mode",
    "quality_evaluation_fail_open",
    "quality_caveat_admission_enabled",
    "quality_gap_recovery_max_attempts",
    "report_review_enabled",
    "report_review_model",
    "report_review_model_max_tokens",
    "report_review_temperature",
    "report_review_max_input_chars",
    "report_review_max_revisions",
    "report_review_fail_open",
    "enable_memory",
    "memory_top_k",
    "memory_min_confidence",
    "memory_auto_write",
    "memory_write_after_report",
    "memory_fail_open",
    "memory_advanced_enabled",
    "memory_decay_enabled",
    "memory_reflection_enabled",
    "memory_profile_enabled",
    "memory_legacy_recall_enabled",
    "memory_run_end_maintenance_enabled",
    "memory_mutation_lock_timeout_seconds",
    "memory_soft_forgetting_enabled",
    "memory_verified_insights_enabled",
    "memory_search_threshold",
    "memory_search_rerank",
    "memory_importance_weight",
    "memory_relevance_weight",
    "memory_recency_weight",
    "memory_reflection_observation_threshold",
    "memory_reflection_importance_threshold",
    "memory_reflection_max_age_hours",
    "memory_maintenance_max_input_chars",
    "memory_profile_max_chars",
    "memory_half_life_days",
)


@router.get("/capabilities")
async def get_capabilities(
    user: Principal = Depends(require_active_user),
) -> dict[str, Any]:
    """Return the safe, explicit browser-editable runtime contract."""
    schema = Configuration.model_json_schema()
    properties = schema.get("properties", {})
    selected = {
        key: properties[key]
        for key in FRONTEND_EDITABLE_CONFIG_KEYS
        if key in properties
    }
    defaults = Configuration.from_runnable_config(None).model_dump(mode="json")
    document_state = await document_health()
    document_settings = get_document_settings()
    publisher_settings = get_publisher_settings()
    return {
        "public_event_schema_version": PUBLIC_EVENT_SCHEMA_VERSION,
        "public_task_activity_schema_version": PUBLIC_TASK_ACTIVITY_SCHEMA_VERSION,
        "publication_schema_version": PUBLICATION_SCHEMA_VERSION,
        "publication_event_schema_version": PUBLICATION_EVENT_SCHEMA_VERSION,
        "accepted_event_schema_versions": [1, PUBLIC_EVENT_SCHEMA_VERSION],
        "features": {
            "clarification": True,
            "human_in_loop": True,
            "feedback": True,
            "artifacts": True,
            "memory": True,
            "subagent_activity": True,
            "subagent_activity_preview": _task_activity_preview_allowed(user),
            "report_review": bool(defaults.get("report_review_enabled", False)),
            "local_dev_auth_bypass": os.environ.get("LOCAL_DEV_AUTH_BYPASS", "").lower()
            in {"1", "true", "yes"},
            "document_research": document_state,
            "publications": {
                "enabled": publisher_settings.enabled,
                "worker": (
                    "ready" if worker_available(publisher_settings) else "degraded"
                ),
            },
        },
        "publication_formats": sorted(PUBLICATION_FORMATS),
        "publication_theme_schema": PublisherTheme.model_json_schema(),
        "publication_theme_defaults": PublisherTheme().model_dump(mode="json"),
        "source_modes": ["web", "documents", "hybrid", "specific"],
        "documents": {
            "formats": [
                "pdf",
                "docx",
                "xlsx",
                "pptx",
                "csv",
                "md",
                "txt",
                "png",
                "jpg",
                "jpeg",
                "tif",
                "tiff",
            ],
            "max_file_bytes": document_settings.max_file_bytes,
            "max_documents_per_user": document_settings.max_documents_per_user,
            "max_bytes_per_user": document_settings.max_bytes_per_user,
            "ocr_enabled": document_settings.ocr_enabled,
            "ocr_mode": document_settings.ocr_mode,
            "ocr_configured": (
                document_settings.ocr_mode == "local"
                or document_settings.remote_ocr_configured
            ),
        },
        "editable_config_keys": list(selected),
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": selected,
            "$defs": schema.get("$defs", {}),
        },
        "defaults": {key: defaults[key] for key in selected if key in defaults},
        "ui": {
            key: (Configuration.model_fields[key].json_schema_extra or {})
            for key in selected
        },
    }


_MODEL_ROLE_FIELDS = (
    "supervisor_model",
    "research_model",
    "summarization_model",
    "message_summary_model",
    "web_rerank_model",
    "web_evidence_model",
    "compression_model",
    "final_report_model",
    "quality_evaluation_model",
    "report_review_model",
)


_MODEL_CATALOG_CACHE_TTL_SECONDS = 60.0


_model_catalog_cache: dict[str, Any] | None = None


_model_catalog_cache_lock = asyncio.Lock()


def _model_catalog_role_aliases(configurable: Configuration) -> dict[str, str]:
    """Project current role -> gateway alias mappings for the settings UI."""
    aliases: dict[str, str] = {}
    for field_name in _MODEL_ROLE_FIELDS:
        value = getattr(configurable, field_name, None)
        if field_name == "report_review_model" and not value:
            value = getattr(configurable, "quality_evaluation_model", None)
        if value:
            aliases[field_name] = str(value)
    return aliases


async def _load_model_catalog_payload() -> dict[str, Any]:
    """Fetch the LiteLLM catalog behind a small process-level TTL cache.

    Gateway failures never raise: a stale-but-usable snapshot wins over an
    empty response, and a cold cache degrades to an empty model list so the
    settings UI can fall back to free-text input.
    """
    global _model_catalog_cache
    now = time.time()
    cached = _model_catalog_cache
    if (
        cached is not None
        and now - cached["loaded_at"] < _MODEL_CATALOG_CACHE_TTL_SECONDS
    ):
        return {**cached["payload"], "stale": False}
    async with _model_catalog_cache_lock:
        cached = _model_catalog_cache
        if (
            cached is not None
            and time.time() - cached["loaded_at"] < _MODEL_CATALOG_CACHE_TTL_SECONDS
        ):
            return {**cached["payload"], "stale": False}
        try:
            settings = RunKeySettings.from_env()
        except LiteLLMKeyConfigurationError:
            if cached is not None:
                return {**cached["payload"], "stale": True}
            return {"models": [], "error": "gateway_not_configured", "stale": True}
        client = LiteLLMModelCatalogClient(
            base_url=settings.base_url,
            api_key=settings.master_key,
        )
        try:
            catalog = await client.load()
        except httpx.HTTPError, ModelCatalogError:
            if cached is not None:
                return {**cached["payload"], "stale": True}
            return {"models": [], "error": "gateway_unavailable", "stale": True}
        finally:
            await client.aclose()
        models = [
            {
                "name": entry.model_name,
                "base_model": entry.base_model,
                "context_window": entry.context_window,
                "max_output_tokens": entry.max_output_tokens,
                "input_cost_per_token": entry.input_cost_per_token,
                "output_cost_per_token": entry.output_cost_per_token,
            }
            for entry in sorted(catalog.values(), key=lambda item: item.model_name)
        ]
        payload: dict[str, Any] = {"models": models, "error": None}
        _model_catalog_cache = {"loaded_at": time.time(), "payload": payload}
        return {**payload, "stale": False}


@router.get("/models")
async def get_model_catalog(
    user: Principal = Depends(require_active_user),
) -> dict[str, Any]:
    """Expose the gateway model catalog so settings can render safe dropdowns."""
    configurable = Configuration.from_runnable_config(None)
    response: dict[str, Any] = {
        "backend": str(configurable.model_backend),
        "role_aliases": _model_catalog_role_aliases(configurable),
    }
    if configurable.model_backend != "litellm":
        response.update({"models": [], "error": None, "stale": False})
        return response
    response.update(await _load_model_catalog_payload())
    return response
