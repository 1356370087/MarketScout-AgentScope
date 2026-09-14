"""Frozen run-contract schema compatibility for historical manifests."""

from __future__ import annotations

import pytest

from open_deep_research.configuration import (
    RUN_CONFIG_FROZEN_FIELDS,
    RUN_CONFIG_FROZEN_FIELDS_V7,
    RUN_CONFIG_FROZEN_FIELDS_V9,
    RUN_CONFIG_FROZEN_FIELDS_V10,
    RUN_CONFIG_FROZEN_FIELDS_V11,
    RUN_CONFIG_FROZEN_FIELDS_V12,
    RUN_CONFIG_MIN_RESUMABLE_SCHEMA_VERSION,
    RUN_CONFIG_SCHEMA_VERSION,
    Configuration,
    freeze_run_config,
    run_config_fingerprint,
)

_V8_ONWARD = set(RUN_CONFIG_FROZEN_FIELDS) - set(RUN_CONFIG_FROZEN_FIELDS_V7)
_V10_ONWARD = set(RUN_CONFIG_FROZEN_FIELDS_V10) - set(RUN_CONFIG_FROZEN_FIELDS_V9)
_V11_ONWARD = set(RUN_CONFIG_FROZEN_FIELDS_V11) - set(RUN_CONFIG_FROZEN_FIELDS_V10)
_V12_ONWARD = set(RUN_CONFIG_FROZEN_FIELDS_V12) - set(RUN_CONFIG_FROZEN_FIELDS_V11)
_V13_ONWARD = set(RUN_CONFIG_FROZEN_FIELDS) - set(RUN_CONFIG_FROZEN_FIELDS_V12)


def _frozen_config(schema_version: int, *, genuine_v7: bool) -> dict:
    configurable = Configuration().model_dump()
    if genuine_v7:
        for field_name in _V8_ONWARD:
            configurable.pop(field_name, None)
    config = {
        "configurable": configurable,
        "metadata": {
            "runtime_config_frozen": True,
            "run_config_schema_version": schema_version,
        },
    }
    config["metadata"]["run_config_fingerprint"] = run_config_fingerprint(config)
    return config


def test_v7_manifest_resumes_with_its_own_frozen_contract() -> None:
    frozen = freeze_run_config(_frozen_config(7, genuine_v7=True))
    assert frozen["metadata"]["run_config_schema_version"] == 7
    # The v8+ fields were absent from the manifest and stay absent instead of
    # failing the resume completeness check.
    assert "model_backend" not in frozen["configurable"]
    assert "sandbox_egress_approval_mode" not in frozen["configurable"]


def test_v9_manifest_requires_v9_fields() -> None:
    with pytest.raises(ValueError, match="frozen_run_config_incomplete"):
        freeze_run_config(_frozen_config(9, genuine_v7=True))


def test_v9_manifest_resumes_without_v10_fields() -> None:
    configurable = Configuration().model_dump()
    for field_name in _V10_ONWARD:
        configurable.pop(field_name, None)
    config = {
        "configurable": configurable,
        "metadata": {
            "runtime_config_frozen": True,
            "run_config_schema_version": 9,
        },
    }
    config["metadata"]["run_config_fingerprint"] = run_config_fingerprint(config)
    frozen = freeze_run_config(config)
    assert frozen["metadata"]["run_config_schema_version"] == 9
    assert "sandbox_egress_approval_mode" not in frozen["configurable"]


def test_v11_manifest_resumes_without_v12_fields() -> None:
    configurable = Configuration().model_dump()
    for field_name in _V12_ONWARD:
        configurable.pop(field_name, None)
    config = {
        "configurable": configurable,
        "metadata": {
            "runtime_config_frozen": True,
            "run_config_schema_version": 11,
        },
    }
    config["metadata"]["run_config_fingerprint"] = run_config_fingerprint(config)

    frozen = freeze_run_config(config)

    assert frozen["metadata"]["run_config_schema_version"] == 11
    assert "approval_pending_turn_allowance" not in frozen["configurable"]
    assert "sandbox_egress_pending_wait_seconds" not in frozen["configurable"]


def test_schema_below_minimum_is_rejected_with_accurate_message() -> None:
    assert RUN_CONFIG_MIN_RESUMABLE_SCHEMA_VERSION == 7
    with pytest.raises(
        ValueError,
        match=r"run_schema_not_resumable:version_6_below_7",
    ):
        freeze_run_config(_frozen_config(6, genuine_v7=True))


def test_v7_frozen_set_matches_historical_contract() -> None:
    """The derived v7 set must not contain fields introduced in v8+."""
    assert RUN_CONFIG_SCHEMA_VERSION == 13
    assert _V8_ONWARD == {
        "model_backend",
        "litellm_policy_revision",
        "model_catalog_snapshot",
        "supervisor_model",
        "quality_evaluation_temperature",
        "gateway_tool_model_max_concurrency",
    } | _V10_ONWARD | _V11_ONWARD | _V12_ONWARD | _V13_ONWARD


def test_v10_frozen_set_contains_egress_auto_mode_contract() -> None:
    """The v10 delta covers the egress classifier and mode settings."""
    assert _V10_ONWARD == {
        "egress_classifier_model",
        "egress_classifier_stages",
        "egress_classifier_timeout_seconds",
        "egress_classifier_max_calls_per_run",
        "egress_classifier_max_consecutive_failures",
        "sandbox_egress_approval_mode",
    }


def test_v11_frozen_set_contains_report_review_contract() -> None:
    """The v11 delta covers final-report Reviewer settings."""
    assert _V11_ONWARD == {
        "report_review_enabled",
        "report_review_model",
        "report_review_model_max_tokens",
        "report_review_temperature",
        "report_review_max_input_chars",
        "report_review_max_revisions",
        "report_review_fail_open",
    }


def test_v12_frozen_set_contains_approval_recovery_contract() -> None:
    assert _V12_ONWARD == {
        "approval_pending_turn_allowance",
        "sandbox_egress_pending_wait_seconds",
    }
