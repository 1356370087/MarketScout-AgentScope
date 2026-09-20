"""Run Key allowlist must cover every Router fallback target in litellm.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from open_deep_research.agentscope_runtime.production_resources import allowed_models
from open_deep_research.configuration import Configuration

ROOT = Path(__file__).resolve().parents[1]


def _litellm_yaml() -> dict:
    with (ROOT / "config" / "litellm.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _alias_configuration() -> Configuration:
    """Mirror the deployment's role-alias mapping from compose/.env.example."""
    return Configuration(
        supervisor_model="if-supervisor-v1",
        research_model="if-research-v1",
        summarization_model="if-summarization-v1",
        message_summary_model="if-message-summary-v1",
        compression_model="if-compression-v1",
        final_report_model="if-final-report-v1",
        quality_evaluation_model="if-quality-v1",
        web_rerank_model="if-web-rerank-v1",
        web_evidence_model="if-web-evidence-v1",
    )


def _fallback_targets(payload: dict) -> set[str]:
    targets: set[str] = set()
    for entry in payload.get("litellm_settings", {}).get("fallbacks", []) or []:
        for _source, destinations in entry.items():
            targets.update(destinations)
    return targets


def _context_window_targets(payload: dict) -> set[str]:
    targets: set[str] = set()
    for entry in (
        payload.get("litellm_settings", {}).get("context_window_fallbacks", []) or []
    ):
        for _source, destinations in entry.items():
            targets.update(destinations)
    return targets


def test_router_fallback_targets_are_whitelisted_on_run_keys() -> None:
    payload = _litellm_yaml()
    model_groups = {
        entry["model_name"]
        for entry in payload.get("model_list", [])
        if isinstance(entry, dict) and entry.get("model_name")
    }
    allowlist = set(allowed_models(_alias_configuration()))

    fallback_targets = _fallback_targets(payload)
    assert fallback_targets, "litellm.yaml must declare cross-group fallbacks"
    # Every fallback target is a declared model group reachable by a Run Key.
    assert fallback_targets <= model_groups
    assert fallback_targets <= allowlist

    context_targets = _context_window_targets(payload)
    assert context_targets <= model_groups
    assert context_targets <= allowlist


def test_fallback_alias_constant_matches_yaml() -> None:
    payload = _litellm_yaml()
    fallback_targets = _fallback_targets(payload)
    # Keep the dedicated fallback alias in sync with the Router policy.
    assert "if-fallback-v1" in fallback_targets
