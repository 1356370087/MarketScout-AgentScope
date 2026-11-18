"""Run Key allowlist must cover every Router fallback target in litellm.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from open_deep_research.agentscope_runtime.production_resources import allowed_models
from open_deep_research.configuration import Configuration
from open_deep_research.platform import runtime as platform_runtime
from open_deep_research.platform.components import model_aliases

ROOT = Path(__file__).resolve().parents[1]


def _litellm_yaml() -> dict:
    with (ROOT / "config" / "litellm.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _alias_configuration(aliases: dict[str, str]) -> Configuration:
    """Mirror the deployment's role-alias mapping from compose/.env.example."""
    return Configuration(
        supervisor_model=aliases["if-supervisor-v2"],
        research_model=aliases["if-research-v2"],
        summarization_model=aliases["if-summarization-v2"],
        message_summary_model=aliases["if-message-summary-v2"],
        compression_model=aliases["if-compression-v2"],
        final_report_model=aliases["if-final-report-v2"],
        quality_evaluation_model=aliases["if-quality-v2"],
        web_rerank_model=aliases["if-web-rerank-v2"],
        web_evidence_model=aliases["if-web-evidence-v2"],
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


def test_router_fallback_targets_are_whitelisted_on_run_keys(monkeypatch) -> None:
    payload = _litellm_yaml()
    document = {"litellm": payload}
    aliases = model_aliases(document)
    monkeypatch.setattr(platform_runtime, "enabled", lambda: True)
    monkeypatch.setattr(platform_runtime, "snapshot", lambda: {"document": document})
    model_groups = {
        entry["model_name"]
        for entry in payload.get("model_list", [])
        if isinstance(entry, dict) and entry.get("model_name")
    }
    allowlist = set(allowed_models(_alias_configuration(aliases)))

    fallback_targets = _fallback_targets(payload)
    assert fallback_targets, "litellm.yaml must declare cross-group fallbacks"
    # Every fallback target is a declared model group reachable by a Run Key.
    assert fallback_targets <= model_groups
    assert {aliases[name] for name in fallback_targets} <= allowlist
    assert "if-fallback-v1" not in allowlist

    context_targets = _context_window_targets(payload)
    assert context_targets <= model_groups
    assert {aliases[name] for name in context_targets} <= allowlist


def test_fallback_alias_constant_matches_yaml() -> None:
    payload = _litellm_yaml()
    fallback_targets = _fallback_targets(payload)
    # Keep the dedicated fallback alias in sync with the Router policy.
    assert "if-fallback-v2" in fallback_targets


def test_published_custom_fallback_and_context_fallback_are_authorized(monkeypatch) -> None:
    payload = _litellm_yaml()
    aliases = model_aliases({"litellm": payload})
    payload["model_list"].extend([
        {"model_name": "custom-backup", "litellm_params": {"model": "provider/backup"}},
        {"model_name": "long-context", "litellm_params": {"model": "provider/long"}},
    ])
    payload["litellm_settings"].update(
        fallbacks=[{"if-research-v2": ["custom-backup"]}],
        context_window_fallbacks=[{"if-research-v2": ["long-context"]}],
    )
    document = {"litellm": payload}
    monkeypatch.setattr(platform_runtime, "enabled", lambda: True)
    monkeypatch.setattr(platform_runtime, "snapshot", lambda: {"document": document})
    physical = model_aliases(document)
    allowed = set(allowed_models(_alias_configuration(aliases)))
    assert {physical["custom-backup"], physical["long-context"]} <= allowed
    assert "custom-backup" not in allowed
