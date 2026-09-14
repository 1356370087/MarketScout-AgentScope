"""LiteLLM-backed evaluation Judge construction tests."""

from __future__ import annotations

import pytest

from open_deep_research.evaluation import judge
from open_deep_research.models.gateway import LiteLLMModelGateway


def test_judge_uses_versioned_alias_and_service_key(monkeypatch) -> None:
    monkeypatch.setenv("EVALUATION_MODEL", "if-evaluation-v2")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm-proxy:4000/v1")
    monkeypatch.setenv("LITELLM_SERVICE_KEY", "service-key")

    resolved = judge.JudgeConfig.from_env()

    assert resolved.provider == "litellm"
    assert resolved.model == "if-evaluation-v2"
    assert resolved.max_retries == 0
    assert resolved.api_key == "service-key"
    assert "service-key" not in repr(resolved)


def test_judge_builds_shared_model_gateway() -> None:
    gateway = judge.build_judge_model(
        judge.JudgeConfig(
            model="if-evaluation-v1",
            api_key="service-key",
            base_url="http://litellm-proxy:4000/v1",
        )
    )

    assert isinstance(gateway, LiteLLMModelGateway)


def test_judge_applies_dashscope_non_thinking_options(monkeypatch) -> None:
    captured = {}

    class CapturingGateway:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(judge, "LiteLLMModelGateway", CapturingGateway)

    judge.build_judge_model(
        judge.JudgeConfig(
            model="qwen3.7-plus-2026-05-26",
            api_key="service-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
    )

    assert captured["extra_body"] == {"enable_thinking": False}


def test_judge_rejects_missing_service_key() -> None:
    with pytest.raises(ValueError, match="LITELLM_SERVICE_KEY"):
        judge.build_judge_model(
            judge.JudgeConfig(
                model="if-evaluation-v1",
                api_key=None,
                base_url="http://litellm-proxy:4000/v1",
            )
        )


def test_judge_rejects_sdk_retries() -> None:
    with pytest.raises(ValueError, match="retries must be zero"):
        judge.build_judge_model(
            judge.JudgeConfig(
                model="if-evaluation-v1",
                api_key="service-key",
                base_url="http://litellm-proxy:4000/v1",
                max_retries=1,
            )
        )
