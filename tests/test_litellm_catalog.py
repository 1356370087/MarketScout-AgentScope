"""LiteLLM model catalog validation tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from open_deep_research.models.catalog import (
    ModelCatalogError,
    parse_model_info,
    validate_model_catalog,
)

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_uses_conservative_multi_deployment_limits_and_prices() -> None:
    catalog = parse_model_info(
        {
            "data": [
                {
                    "model_name": "if-research-v1",
                    "model_info": {
                        "base_model": "openai/gpt-4.1",
                        "max_input_tokens": 100_000,
                        "max_output_tokens": 32_000,
                        "input_cost_per_token": 0.000001,
                        "output_cost_per_token": 0.000004,
                    },
                },
                {
                    "model_name": "if-research-v1",
                    "model_info": {
                        "base_model": "openai/gpt-4.1",
                        "max_input_tokens": 80_000,
                        "max_output_tokens": 16_000,
                        "input_cost_per_token": 0.000002,
                        "output_cost_per_token": 0.000005,
                    },
                },
            ]
        }
    )
    entry = catalog["if-research-v1"]
    assert entry.context_window == 80_000
    assert entry.max_output_tokens == 16_000
    assert entry.input_cost_per_token == 0.000002
    assert entry.output_cost_per_token == 0.000005


def test_catalog_rejects_missing_and_unknown_price() -> None:
    with pytest.raises(ModelCatalogError, match="model_info_missing"):
        validate_model_catalog({}, ["if-research-v1"], budget_enabled=True)
    catalog = parse_model_info(
        {
            "data": [
                {
                    "model_name": "if-research-v1",
                    "model_info": {
                        "max_input_tokens": 10,
                        "max_output_tokens": 5,
                        "input_cost_per_token": 0,
                        "output_cost_per_token": 0,
                    },
                }
            ]
        }
    )
    with pytest.raises(ModelCatalogError, match="model_price_unknown"):
        validate_model_catalog(catalog, ["if-research-v1"], budget_enabled=True)


def test_versioned_litellm_config_has_complete_alias_and_fallback_references() -> None:
    config = yaml.safe_load((ROOT / "config" / "litellm.yaml").read_text(encoding="utf-8"))
    deployments = config["model_list"]
    names = {deployment["model_name"] for deployment in deployments}
    expected = {
        "if-supervisor-v1",
        "if-research-v1",
        "if-summarization-v1",
        "if-message-summary-v1",
        "if-web-rerank-v1",
        "if-web-evidence-v1",
        "if-compression-v1",
        "if-final-report-v1",
        "if-quality-v1",
        "if-report-review-v1",
        "if-evaluation-v1",
        "if-fallback-v1",
    }

    assert expected <= names
    assert sum(item["model_name"] == "if-research-v1" for item in deployments) == 2
    assert all(item["model_info"].get("base_model") for item in deployments)
    quality_aliases = {
        "if-quality-v1",
        "if-report-review-v1",
        "if-evaluation-v1",
    }
    assert {
        item["litellm_params"]["model"]
        for item in deployments
        if item["model_name"] in quality_aliases
    } == {"zai/glm-5.3-flash"}
    # GLM 的元数据尚未内置于固定版本的 LiteLLM，必须显式声明。
    glm_deployments = [
        item for item in deployments
        if item["litellm_params"]["model"] == "zai/glm-5.3-flash"
    ]
    catalog = parse_model_info({"data": glm_deployments})
    validate_model_catalog(
        catalog, [item["model_name"] for item in glm_deployments],
        budget_enabled=True,
    )
    for mapping in config["litellm_settings"]["fallbacks"]:
        source, targets = next(iter(mapping.items()))
        assert source in names
        assert set(targets) <= names
    for mapping in config["litellm_settings"]["context_window_fallbacks"]:
        source, targets = next(iter(mapping.items()))
        assert source in names
        assert set(targets) <= names

    assert config["router_settings"]["routing_strategy"] == "simple-shuffle"
    assert config["litellm_settings"]["num_retries"] == 2
    assert config["litellm_settings"]["request_timeout"] == 180
    assert config["litellm_settings"]["turn_off_message_logging"] is True


def test_env_example_uses_litellm_aliases_and_hides_legacy_transport_controls() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    active = {
        key: value
        for line in lines
        if line and not line.startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
    }
    aliases = (
        "SUPERVISOR_MODEL",
        "RESEARCH_MODEL",
        "SUMMARIZATION_MODEL",
        "MESSAGE_SUMMARY_MODEL",
        "WEB_RERANK_MODEL",
        "WEB_EVIDENCE_MODEL",
        "COMPRESSION_MODEL",
        "FINAL_REPORT_MODEL",
        "QUALITY_EVALUATION_MODEL",
        "EVALUATION_MODEL",
    )
    legacy_only = (
        "MODEL_FALLBACKS",
        "MODEL_TRANSPORT_MAX_ATTEMPTS",
        "MODEL_CIRCUIT_BREAKER_ENABLED",
        "MODEL_FIRST_PACKET_PROBE",
        "MODEL_COSTS_PER_MILLION",
        "QUALITY_EVALUATION_BASE_URL",
        "QUALITY_EVALUATION_API_KEY",
        "EVALUATION_BASE_URL",
        "EVALUATION_API_KEY",
        "LANGFUSE_LANGCHAIN_CALLBACK_ENABLED",
        "GET_API_KEYS_FROM_CONFIG",
    )

    assert active["MODEL_BACKEND"] == "litellm"
    assert all(active[name].startswith("if-") and ":" not in active[name] for name in aliases)
    assert int(active["LITELLM_RUN_BUDGET_DEFAULT_MICRO_USD"]) > 0
    assert int(active["LITELLM_RUN_BUDGET_MAX_MICRO_USD"]) >= int(
        active["LITELLM_RUN_BUDGET_DEFAULT_MICRO_USD"]
    )
    assert int(active["LITELLM_SERVICE_BUDGET_MICRO_USD"]) > 0
    assert re.fullmatch(r"\d+(s|m|h|d|mo)", active["LITELLM_SERVICE_BUDGET_DURATION"])
    assert not set(legacy_only) & active.keys()


def test_litellm_config_never_embeds_literal_provider_secrets() -> None:
    """Guard against hardcoded API keys leaking into the tracked proxy config."""
    raw = (ROOT / "config" / "litellm.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(raw)
    for deployment in config["model_list"]:
        api_key = deployment["litellm_params"].get("api_key", "")
        assert isinstance(api_key, str) and api_key.startswith("os.environ/"), (
            f"deployment {deployment['model_name']} must reference env vars, "
            f"got: {api_key!r}"
        )
    assert "sk-" not in raw, "literal provider key material must not appear in the config"


def test_litellm_otel_variant_differs_only_in_callbacks() -> None:
    """The OTel config copy must track the main config except for callbacks."""
    main = yaml.safe_load((ROOT / "config" / "litellm.yaml").read_text(encoding="utf-8"))
    otel = yaml.safe_load(
        (ROOT / "config" / "litellm.otel.yaml").read_text(encoding="utf-8")
    )
    for section in ("model_list", "router_settings", "general_settings"):
        assert otel[section] == main[section], (
            f"config/litellm.otel.yaml drifted from litellm.yaml in {section}; "
            "regenerate the derived copy"
        )
    assert otel["litellm_settings"]["callbacks"] == ["otel", "prometheus"]
    assert main["litellm_settings"]["callbacks"] == ["prometheus"]
    for key, value in main["litellm_settings"].items():
        if key == "callbacks":
            continue
        assert otel["litellm_settings"][key] == value
