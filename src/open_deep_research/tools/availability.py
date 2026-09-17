"""Declarative availability predicates shared by built-in tool definitions."""

from __future__ import annotations

from open_deep_research.config_types import RuntimeConfig

from open_deep_research.configuration import Configuration, SearchAPI
from open_deep_research.sandbox.policy import network_policy_mode


def _configuration(config: RuntimeConfig) -> Configuration:
    return Configuration.from_runnable_config(config)


def provider_search_enabled(config: RuntimeConfig, provider: SearchAPI) -> bool:
    """Return whether a legacy/shadow provider search tool is enabled."""
    configurable = _configuration(config)
    return (
        network_policy_mode(configurable) != "offline"
        and configurable.web_pipeline_mode in {"legacy", "shadow"}
        and SearchAPI(configurable.search_api) is provider
    )


def legacy_fetch_enabled(config: RuntimeConfig) -> bool:
    """Return whether the legacy webpage fetcher is enabled."""
    configurable = _configuration(config)
    return (
        network_policy_mode(configurable) != "offline"
        and configurable.web_pipeline_mode in {"legacy", "shadow"}
    )


def enforced_pipeline_enabled(config: RuntimeConfig) -> bool:
    """Return whether enforced pipeline tools are enabled."""
    configurable = _configuration(config)
    return (
        network_policy_mode(configurable) != "offline"
        and configurable.web_pipeline_mode == "enforced"
    )
