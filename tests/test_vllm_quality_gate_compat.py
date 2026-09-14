"""Regressions for the round-8 E2E findings around self-hosted Qwen judges.

Root cause fixed here: ``langchain_openai`` routes any ``response_format``
request through the OpenAI SDK's ``parse()`` helper, which raises
``LengthFinishReasonError`` when the generation hits ``finish_reason ==
"length"``. The runtime quality gate bound DashScope-style JSON mode for
every ``qwen*`` model name regardless of endpoint, so a vLLM-hosted Qwen
judge with a 2048-token ceiling failed every real evaluation while minimal
probes succeeded.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from open_deep_research.agents.deep_researcher import (
    _FETCH_BUDGET_EXHAUSTION_STREAK_LIMIT,
    fetch_budget_exhausted_iteration_streak,
)
from open_deep_research.completion import (
    CompletionDecision,
    ResearchCompletionPolicy,
    completion_policy_context,
)
from open_deep_research.configuration import Configuration
from open_deep_research.models.fallback import (
    ModelErrorKind,
    classify_model_error,
)
from open_deep_research.models.resolution import (
    build_model_config,
    is_dashscope_qwen,
    resolve_compatibility_kwargs,
)
from open_deep_research.quality.gate import _build_quality_model


class _StubLengthFinishReasonError(Exception):
    """Mirror of openai.LengthFinishReasonError without importing the SDK."""


def test_length_finish_reason_error_classifies_as_output_truncated() -> None:
    error = _StubLengthFinishReasonError(
        "Could not parse response content as the length limit was reached"
    )
    assert classify_model_error(error) is ModelErrorKind.OUTPUT_TRUNCATED


def test_openai_length_finish_reason_error_classifies_as_output_truncated() -> None:
    pytest.importorskip("openai")
    from openai import LengthFinishReasonError

    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=5)
    completion = SimpleNamespace(usage=usage)
    assert (
        classify_model_error(
            LengthFinishReasonError(completion=completion)  # type: ignore[arg-type]
        )
        is ModelErrorKind.OUTPUT_TRUNCATED
    )


def test_is_dashscope_qwen_requires_dashscope_endpoint() -> None:
    assert is_dashscope_qwen("openai:qwen3.7-plus")
    assert is_dashscope_qwen(
        "openai:qwen3.7-plus",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    assert is_dashscope_qwen(
        "openai:any-model",
        "https://wk.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    # A self-hosted OpenAI-compatible endpoint is not DashScope even when it
    # serves a Qwen checkpoint under a qwen-prefixed name.
    assert not is_dashscope_qwen(
        "openai:Qwen/Qwen3.8-27B-FP8",
        "http://host.docker.internal:18081/v1",
    )


def test_self_hosted_qwen_disables_thinking_via_chat_template_kwargs() -> None:
    vllm_url = "http://172.22.121.109:8080/v1"
    assert resolve_compatibility_kwargs("openai:Qwen/x", base_url=vllm_url) == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
    }
    # DashScope endpoints keep the documented top-level parameter.
    assert resolve_compatibility_kwargs("openai:qwen-plus") == {
        "extra_body": {"enable_thinking": False}
    }


def test_build_model_config_uses_endpoint_aware_qwen_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "false")
    config = build_model_config(
        "openai:Qwen/Qwen3.8-27B-FP8",
        8192,
        {},
        role="quality_evaluation",
        configured_base_url="http://172.22.121.109:8080/v1",
        temperature=0.1,
    )
    assert config["base_url"] == "http://172.22.121.109:8080/v1"
    assert config["temperature"] == 0.1
    assert config["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "enable_thinking" not in config["extra_body"]


def test_quality_model_skips_json_mode_binding_for_self_hosted_qwen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class FakeModel:
        def with_config(self, kwargs):
            captured["init"] = kwargs
            return self

        def bind(self, **kwargs):
            captured["bind"] = kwargs
            return self

    monkeypatch.setattr(
        "open_deep_research.models.resolution.get_configurable_model_template",
        lambda: FakeModel(),
    )
    configurable = Configuration(
        quality_evaluation_model="openai:Qwen/Qwen3.8-27B-FP8",
        quality_evaluation_base_url="http://host.docker.internal:18081/v1",
    )

    _build_quality_model(configurable, {"configurable": {}})

    assert captured["init"]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert "bind" not in captured


def test_completion_policy_reports_fetch_budget_exhausted_reason() -> None:
    policy = ResearchCompletionPolicy(min_evidence=1, min_sources=0)
    result = policy.evaluate(
        completion_policy_context(
            {"evidence_registry": [], "raw_notes": []},
            explicit_completion_succeeded=False,
            explicit_completion_failed=False,
            has_remaining_budget=False,
            exhausted_reason="fetch_budget_exhausted",
        )
    )
    assert result.action is CompletionDecision.TERMINATE
    assert result.reason == "fetch_budget_exhausted"


def test_fetch_budget_streak_requires_structured_gap_telemetry() -> None:
    exhausted_iteration = {
        "gap_analysis": {
            "budget": {
                "fetch_attempts": 0,
                "fetched_documents": 0,
                "reserved_fetches": 0,
                "exhaustion_scope": "run",
            }
        }
    }
    # fetch_url-shaped records without gap_analysis must not count toward the
    # deterministic streak, and the limit matches the runtime constant.
    assert fetch_budget_exhausted_iteration_streak(
        [exhausted_iteration] * _FETCH_BUDGET_EXHAUSTION_STREAK_LIMIT
    ) >= _FETCH_BUDGET_EXHAUSTION_STREAK_LIMIT
    assert (
        fetch_budget_exhausted_iteration_streak(
            [{"request": {}}, exhausted_iteration]
        )
        == 1
    )


def test_resolved_run_identity_falls_back_to_task_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_deep_research.tools.web_research import pipeline

    config = {"metadata": {"task_id": "task-1"}}
    assert pipeline.resolved_run_identity({"metadata": {"run_id": "run-9"}}) == "run-9"

    class _Registry:
        def get(self, task_id: str):
            assert task_id == "task-1"
            return SimpleNamespace(run_id="run-from-registry")

    monkeypatch.setattr(
        "open_deep_research.tasks.registry.get_task_registry",
        lambda: _Registry(),
    )
    assert pipeline.resolved_run_identity(config) == "run-from-registry"
    assert pipeline.resolved_run_identity({"metadata": {}}) == "default"


def test_gateway_failure_usage_extracts_billed_tokens() -> None:
    from open_deep_research.sandbox.gateway import GatewayRuntime

    usage = SimpleNamespace(prompt_tokens=950, completion_tokens=2048)
    completion = SimpleNamespace(usage=usage)
    exc = _StubLengthFinishReasonError("truncated")
    exc.completion = completion  # type: ignore[attr-defined]
    assert GatewayRuntime._failure_usage(exc) == {
        "input_tokens": 950,
        "output_tokens": 2048,
    }
    assert GatewayRuntime._failure_usage(_StubLengthFinishReasonError("x")) == {}


def test_exception_failure_usage_reads_gateway_attribute() -> None:
    from open_deep_research.observability.core import _exception_failure_usage

    error = RuntimeError("sandbox_gateway_model_failed")
    error.failure_usage = {"input_tokens": 10, "output_tokens": 5}  # type: ignore[attr-defined]
    assert _exception_failure_usage(error) == {"input_tokens": 10, "output_tokens": 5}
    assert _exception_failure_usage(RuntimeError("plain")) is None


def test_failed_model_run_records_billed_usage() -> None:
    from open_deep_research.observability.core import UsageCaptureCallback

    capture = UsageCaptureCallback()
    error = RuntimeError("sandbox_gateway_model_failed")
    error.failure_usage = {"input_tokens": 700, "output_tokens": 300}  # type: ignore[attr-defined]
    capture._record_failed_run("run-1", error)
    assert len(capture.records) == 1
    record = capture.records[0]
    assert record.input_tokens == 700
    assert record.output_tokens == 300
    assert record.response_status in {"rejected", "unknown_failed"}


@pytest.mark.asyncio
async def test_gateway_process_forwards_task_activity_to_internal_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_deep_research.events import task_activity

    monkeypatch.setenv("SANDBOX_GATEWAY_PHYSICAL_PROCESS", "true")
    monkeypatch.setenv("SANDBOX_API_INTERNAL_URL", "http://api:2024")
    monkeypatch.setenv("SANDBOX_ROOT_SIGNING_KEY", "root-key")
    posted: list[tuple[str, object]] = []

    class _StubClient:
        def __init__(self, base_url: str, root_key: str) -> None:
            assert base_url == "http://api:2024"
            assert root_key

        def signed(self, model_type, **values):
            return SimpleNamespace(
                model_type=model_type, **values, _payload=values
            )

        async def post(self, path: str, request) -> dict:
            posted.append((path, request))
            return {"published": True}

    monkeypatch.setattr(
        "open_deep_research.sandbox.internal_api.SandboxInternalClient",
        _StubClient,
    )

    await task_activity.publish_task_activity(
        {"metadata": {"run_id": "run-1", "task_id": "task-1"}},
        "model.started",
        kind="model",
        phase="reasoning",
        status="running",
        title="模型规划",
        summary="子代理正在规划。",
        dedupe_key="activity:model:abc:started",
    )

    assert len(posted) == 1
    path, request = posted[0]
    assert path == "/internal/sandbox/task-activity"
    assert request.run_id == "run-1"
    assert request.event_type == "model.started"
    assert request.dedupe_key == "activity:model:abc:started"


def test_task_activity_publish_request_roundtrip() -> None:
    from open_deep_research.sandbox.crypto import (
        SandboxDerivedKeys,
        sign_payload,
    )
    from open_deep_research.sandbox.internal_api import (
        SandboxInternalClient,
        TaskActivityPublishRequest,
    )

    client = SandboxInternalClient("http://api:2024", "r" * 64)
    request = client.signed(
        TaskActivityPublishRequest,
        run_id="run-1",
        task_id="task-1",
        event_type="quality.failed",
        kind="quality",
        phase="quality_check",
        status="error",
        title="质量复核失败",
        summary="调用未能在重试预算内完成。",
        dedupe_key="activity:model:x:failed",
        payload={"evaluation_type": "supervisor.evaluate_handoff"},
    )
    keys = SandboxDerivedKeys.from_root("r" * 64)
    assert sign_payload(request.signed_payload(), keys.service_auth) == (
        request.service_signature
    )
    assert request.payload == {
        "evaluation_type": "supervisor.evaluate_handoff"
    }
