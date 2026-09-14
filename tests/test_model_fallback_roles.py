"""Role-level model fallback policy regression tests."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from open_deep_research.agents.model_recovery import (
    build_model_candidate_chain,
    invoke_with_model_fallback,
)
from open_deep_research.configuration import Configuration
from open_deep_research.events.public import event_store_from_config
from open_deep_research.models.errors import is_token_limit_exceeded
from open_deep_research.models.fallback import ModelErrorKind, classify_model_error


class _InvalidRequestAuthenticationError(Exception):
    status_code = 401
    type = "invalid_request_error"
    code = "invalid_request_error"


class _ContextLengthInvalidRequestError(Exception):
    status_code = 400
    type = "invalid_request_error"
    code = "invalid_request_error"


@pytest.mark.asyncio
async def test_shared_fallback_switches_and_sanitizes_provider_metadata(
    tmp_path,
) -> None:
    calls: list[tuple[str, list]] = []
    events: list[dict[str, str]] = []

    async def invoke(model_id: str, messages: list):
        calls.append((model_id, messages))
        if model_id == "openai:primary":
            raise RuntimeError("model unavailable")
        return AIMessage(content="ok")

    config = {
        "configurable": {"runs_dir": str(tmp_path)},
        "metadata": {"run_id": "fallback-event", "query_turn": 7},
    }
    response = await invoke_with_model_fallback(
        invoke,
        [
            AIMessage(
                content="prior",
                additional_kwargs={"signature": "provider-bound"},
                response_metadata={"reasoning": "provider-bound"},
            ),
            HumanMessage(content="continue"),
        ],
        primary_model="openai:primary",
        model_fallbacks={"compression": ["anthropic:fallback"]},
        role="compression",
        config=config,
        on_fallback=events.append,
    )

    assert response.content == "ok"
    assert [model_id for model_id, _messages in calls] == [
        "openai:primary",
        "anthropic:fallback",
    ]
    replayed = calls[1][1][0]
    assert "signature" not in replayed.additional_kwargs
    assert "reasoning" not in replayed.response_metadata
    assert events == [{
        "turn": 7,
        "from_model": "openai:primary",
        "to_model": "anthropic:fallback",
        "reason": "model_unavailable",
    }]
    public_events = event_store_from_config(config).read()
    fallback_event = next(
        event for event in public_events if event.type == "query.model_fallback"
    )
    assert fallback_event.payload == events[0]


@pytest.mark.asyncio
async def test_shared_fallback_does_not_cross_on_auth_errors() -> None:
    attempted: list[str] = []

    async def invoke(model_id: str, _messages: list):
        attempted.append(model_id)
        raise RuntimeError("invalid api key")

    with pytest.raises(RuntimeError, match="invalid api key"):
        await invoke_with_model_fallback(
            invoke,
            [HumanMessage(content="brief")],
            primary_model="openai:primary",
            model_fallbacks={"quality_evaluation": ["openai:fallback"]},
            role="quality_evaluation",
        )

    assert attempted == ["openai:primary"]


def test_invalid_request_authentication_error_is_not_prompt_too_long() -> None:
    error = _InvalidRequestAuthenticationError(
        "Incorrect API key provided; invalid authentication token. "
        "See help.aliyun.com/zh/model-studio/error-code"
    )

    assert not is_token_limit_exceeded(error, "openai:Qwen/Qwen3.8-27B-FP8")
    assert (
        classify_model_error(error, "openai:Qwen/Qwen3.8-27B-FP8")
        is ModelErrorKind.AUTH
    )


def test_invalid_request_requires_context_evidence_for_prompt_too_long() -> None:
    error = _ContextLengthInvalidRequestError(
        "Maximum context length is 32768 tokens; reduce the length of the prompt."
    )

    assert is_token_limit_exceeded(error, "openai:gpt-4.1")
    assert (
        classify_model_error(error, "openai:gpt-4.1")
        is ModelErrorKind.PROMPT_TOO_LONG
    )


def test_candidate_chain_is_deduplicated_and_uses_role_config() -> None:
    template = object()
    candidates = build_model_candidate_chain(
        "openai:primary",
        ["openai:primary", "anthropic:fallback"],
        max_tokens=4096,
        config={},
        role="supervisor",
        model=template,
    )

    assert [candidate.model_id for candidate in candidates] == [
        "openai:primary",
        "anthropic:fallback",
    ]
    assert all(candidate.model is template for candidate in candidates)
    assert candidates[1].model_config["model"] == "anthropic:fallback"


def test_model_fallbacks_env_json_configures_role_chains(monkeypatch) -> None:
    monkeypatch.setenv(
        "MODEL_FALLBACKS",
        '{"researcher": ["anthropic:fallback", "openai:secondary"]}',
    )

    configurable = Configuration.from_runnable_config({})

    assert configurable.model_fallbacks == {
        "researcher": ["anthropic:fallback", "openai:secondary"],
    }
    candidates = build_model_candidate_chain(
        "openai:primary",
        configurable.model_fallbacks.get("researcher", []),
        max_tokens=4096,
        config={},
        role="researcher",
        model=object(),
    )
    assert [candidate.model_id for candidate in candidates] == [
        "openai:primary",
        "anthropic:fallback",
        "openai:secondary",
    ]


def test_model_fallbacks_env_empty_string_disables_fallback(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_FALLBACKS", "")

    configurable = Configuration.from_runnable_config({})

    assert configurable.model_fallbacks == {}


def test_model_fallbacks_env_rejects_unknown_roles(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_FALLBACKS", '{"lead": ["openai:fallback"]}')

    with pytest.raises(ValidationError, match="unknown model fallback roles"):
        Configuration.from_runnable_config({})


def test_gateway_budget_rejections_classify_as_budget_exceeded() -> None:
    from open_deep_research.models.fallback import _FALLBACK_ERROR_KINDS
    from open_deep_research.models.gateway import ModelGatewayError

    classified = classify_model_error(
        ModelGatewayError("gateway_budget_exceeded", status_code=403)
    )
    assert classified is ModelErrorKind.BUDGET_EXCEEDED

    class _TeamBudgetError(Exception):
        status_code = 429

    assert (
        classify_model_error(_TeamBudgetError("Budget has been exceeded!"))
        is ModelErrorKind.BUDGET_EXCEEDED
    )

    # Plain auth/rate-limit signals keep their original taxonomy.
    class _Forbidden(Exception):
        status_code = 403

    assert classify_model_error(_Forbidden("forbidden")) is ModelErrorKind.AUTH
    assert classify_model_error(_TeamBudgetError("rate limit hit")) is ModelErrorKind.RATE_LIMITED

    # Spending the window is not recoverable by switching models.
    assert ModelErrorKind.BUDGET_EXCEEDED not in _FALLBACK_ERROR_KINDS
