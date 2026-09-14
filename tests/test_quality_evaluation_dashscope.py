"""Configuration and opt-in live tests for the DashScope quality evaluator."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from dotenv import dotenv_values
from langchain_core.messages import HumanMessage, ToolMessage
from openai import OpenAI
from pydantic import BaseModel

from open_deep_research.agents.deep_researcher import (
    assess_research_results,
    prepare_researcher_tool_outcomes,
)
from open_deep_research.configuration import Configuration
from open_deep_research.evaluation import (
    JudgeConfig,
    invoke_judge_structured_sync,
)
from open_deep_research.models.capabilities import dashscope_qwen_enable_thinking
from open_deep_research.models.codec import (
    STRUCTURED_OUTPUT_TOOL_NAME,
    structured_output_tool,
    structured_tool_choice,
)
from open_deep_research.quality.contract import build_research_coverage_contract
from open_deep_research.quality.gate import (
    ToolResultAssessment,
    _build_quality_model,
    _content_text,
    evaluate_subagent_handoff,
    evaluate_tool_results,
)
from open_deep_research.tools.base import ToolOrigin
from open_deep_research.tools.governance import GovernedToolCallResult

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
LIVE_TEST_ENV = "RUN_DASHSCOPE_QUALITY_LIVE_TEST"
LIVE_MODEL_ID = "qwen3.7-plus-2026-05-26"
LIVE_MODEL_SPEC = f"openai:{LIVE_MODEL_ID}"
DASHSCOPE_PUBLIC_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


class _LiveJudgeProbe(BaseModel):
    ok: bool


def _quality_env() -> dict[str, str]:
    """Read only the quality-evaluator settings without exporting secrets."""
    values = dotenv_values(ENV_PATH)
    return {
        key: str(os.getenv(key) or values.get(key) or "").strip()
        for key in (
            "DASHSCOPE_API_KEY",
            "QUALITY_EVALUATION_ENABLED",
            "QUALITY_EVALUATION_MODEL",
            "QUALITY_EVALUATION_BASE_URL",
            "QUALITY_EVALUATION_FAIL_OPEN",
            "QUALITY_EVALUATION_RIGOR",
            "QUALITY_EVALUATION_MIN_SCORE",
            "QUALITY_EVALUATION_MIN_SOURCES",
        )
    }


def _dashscope_test_env() -> dict[str, str]:
    """Return deterministic unit-test settings independent of the developer .env."""
    return {
        "DASHSCOPE_API_KEY": "sk-dashscope-test",
        "QUALITY_EVALUATION_ENABLED": "true",
        "QUALITY_EVALUATION_MODEL": "openai:qwen3.7-flash",
        "QUALITY_EVALUATION_BASE_URL": (
            "https://workspace.cn-beijing.maas.aliyuncs.com/"
            "compatible-mode/v1"
        ),
        "QUALITY_EVALUATION_FAIL_OPEN": "false",
        "QUALITY_EVALUATION_RIGOR": "strict",
        "QUALITY_EVALUATION_MIN_SCORE": "4",
        "QUALITY_EVALUATION_MIN_SOURCES": "2",
    }


def _live_quality_env() -> dict[str, str]:
    """Pin live diagnostics to the requested public DashScope model."""
    values = _quality_env()
    assert values["DASHSCOPE_API_KEY"], "DASHSCOPE_API_KEY is required"
    values.update({
        "QUALITY_EVALUATION_ENABLED": "true",
        "QUALITY_EVALUATION_MODEL": LIVE_MODEL_SPEC,
        "QUALITY_EVALUATION_BASE_URL": DASHSCOPE_PUBLIC_BASE_URL,
        "QUALITY_EVALUATION_FAIL_OPEN": "false",
        "QUALITY_EVALUATION_RIGOR": "balanced",
        "QUALITY_EVALUATION_MIN_SCORE": "",
        "QUALITY_EVALUATION_MIN_SOURCES": "2",
    })
    return values


def _assert_dashscope_base_url(base_url: str) -> None:
    """Validate the deployable form of a DashScope OpenAI-compatible URL."""
    assert base_url, "QUALITY_EVALUATION_BASE_URL is required"
    assert "{" not in base_url and "}" not in base_url, (
        "QUALITY_EVALUATION_BASE_URL still contains template braces; replace "
        "{WorkspaceId} with the raw workspace ID and remove the braces"
    )
    parsed = urlparse(base_url)
    assert parsed.scheme == "https", "QUALITY_EVALUATION_BASE_URL must use HTTPS"
    assert parsed.username is None and parsed.password is None
    assert parsed.query == "" and parsed.fragment == ""
    assert parsed.path.rstrip("/") == "/compatible-mode/v1"
    assert parsed.hostname, "QUALITY_EVALUATION_BASE_URL must contain a hostname"
    assert (
        parsed.hostname == "dashscope.aliyuncs.com"
        or parsed.hostname.endswith(".cn-beijing.maas.aliyuncs.com")
    )
    assert all(_DNS_LABEL_RE.fullmatch(label) for label in parsed.hostname.split("."))


def _install_live_quality_env(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    values = _live_quality_env()
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("QUALITY_EVALUATION_API_KEY", raising=False)
    monkeypatch.setenv("QUALITY_EVALUATION_MODEL_MAX_TOKENS", "4096")
    monkeypatch.setenv("QUALITY_EVALUATION_TEMPERATURE", "0")
    monkeypatch.setenv("QUALITY_EVALUATION_MAX_INPUT_CHARS", "30000")
    return values


def _live_gate_config() -> dict[str, object]:
    return {
        "configurable": {
            "external_content_fail_closed": True,
            "max_mcp_output_chars": 50_000,
            "prompt_injection_protection_enabled": True,
        },
        "metadata": {
            "run_id": "dashscope-quality-gate-lifecycle-live-test",
            "quality_policy_version": "quality-gate-v4",
        },
    }


def _emit_live_observation(
    stage: str,
    result: BaseModel,
    *,
    elapsed_seconds: float,
) -> None:
    payload = result.model_dump(mode="json")
    observation = {
        "stage": stage,
        "model": LIVE_MODEL_ID,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "result": payload,
    }
    print(  # noqa: T201 - live diagnostics are consumed from pytest -s output
        "QUALITY_GATE_LIVE_OBSERVATION="
        + json.dumps(observation, ensure_ascii=False, sort_keys=True)
    )


def _python_free_threading_evidence(
    requirement_ids: list[str],
) -> list[dict[str, object]]:
    return [
        {
            "evidence_id": "ev-pep703-build",
            "claim": (
                "PEP 703 defines a CPython build configuration that disables "
                "the GIL through the --disable-gil option."
            ),
            "supporting_excerpt": (
                "CPython builds without the GIL use the --disable-gil build "
                "configuration option; this is a build-time choice."
            ),
            "source_url": "https://peps.python.org/pep-0703/",
            "source_title": "PEP 703 – Making the Global Interpreter Lock Optional",
            "source_authority": 1.0,
            "security_status": "accepted",
            "requirement_ids": requirement_ids,
        },
        {
            "evidence_id": "ev-py313-experimental",
            "claim": (
                "Python 3.13 provides experimental support for a free-threaded "
                "build as a separate installation option."
            ),
            "supporting_excerpt": (
                "The Python 3.13 documentation describes free-threaded CPython "
                "as experimental and available through a separate build."
            ),
            "source_url": (
                "https://docs.python.org/3.13/whatsnew/3.13.html"
                "#free-threaded-cpython"
            ),
            "source_title": "What’s New In Python 3.13 – Free-threaded CPython",
            "source_authority": 1.0,
            "security_status": "accepted",
            "requirement_ids": requirement_ids,
        },
        {
            "evidence_id": "ev-py313-runtime-gil",
            "claim": (
                "A free-threaded Python process can report whether the GIL is "
                "enabled and may re-enable it when importing incompatible extensions."
            ),
            "supporting_excerpt": (
                "The free-threading HOWTO documents sys._is_gil_enabled() and "
                "explains that importing an extension without free-threading "
                "support can enable the GIL at runtime."
            ),
            "source_url": (
                "https://docs.python.org/3.13/howto/free-threading-python.html"
                "#the-global-interpreter-lock-in-free-threaded-python"
            ),
            "source_title": "Python 3.13 Free-threading HOWTO",
            "source_authority": 1.0,
            "security_status": "accepted",
            "requirement_ids": requirement_ids,
        },
    ]


async def _prepare_governed_web_batch(
    evidence: list[dict[str, object]],
    config: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Drive evidence through the same governance seam as Researcher tools."""
    payload = {
        "request": {
            "objective": "Verify Python 3.13 free-threading boundaries.",
            "queries": ["Python 3.13 free-threading official documentation"],
        },
        "candidates": [
            {
                "candidate_id": f"candidate-{index}",
                "canonical_url": item["source_url"],
                "title": item["source_title"],
            }
            for index, item in enumerate(evidence, start=1)
        ],
        "documents": [
            {
                "document_id": f"document-{index}",
                "final_url": item["source_url"],
                "status": "fetched",
            }
            for index, item in enumerate(evidence, start=1)
        ],
        "evidence": evidence,
        "gap_analysis": {
            "missing": [] if len(evidence) >= 3 else ["runtime behavior"],
        },
    }
    call = {
        "name": "web_research",
        "args": payload["request"],
        "id": "quality-live-web-research",
    }
    outcome = GovernedToolCallResult(
        message=ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            name="web_research",
            tool_call_id=call["id"],
        )
    )
    tool = SimpleNamespace(name="web_research", origin=ToolOrigin.SEARCH)

    _messages, update = await prepare_researcher_tool_outcomes(
        [call],
        [outcome],
        {"web_research": tool},
        config,
    )
    pending = list(update.get("pending_tool_results", []))
    registered = list(update.get("evidence_registry", []))
    assert pending and pending[0]["error"] is False
    assert len(registered) == len(evidence), (
        "governed Web results must populate cumulative evidence before the gate"
    )
    return pending, registered


class TestDashScopeQualityEvaluationConfiguration:
    """Exercise the same configuration seam used by the runtime quality gate."""

    def test_requested_live_model_uses_non_thinking_mode(self) -> None:
        assert dashscope_qwen_enable_thinking(LIVE_MODEL_SPEC) is False

    def test_dashscope_fixture_is_a_valid_openai_compatible_configuration(
        self,
    ) -> None:
        values = _dashscope_test_env()

        assert values["DASHSCOPE_API_KEY"].startswith("sk-"), (
            "DASHSCOPE_API_KEY does not look like a DashScope API key"
        )
        provider, separator, model = values["QUALITY_EVALUATION_MODEL"].partition(":")
        assert separator and provider == "openai"
        assert model and model.strip() == model
        assert values["QUALITY_EVALUATION_ENABLED"].lower() in {"true", "false"}
        assert values["QUALITY_EVALUATION_FAIL_OPEN"].lower() in {"true", "false"}
        if values["QUALITY_EVALUATION_RIGOR"]:
            assert values["QUALITY_EVALUATION_RIGOR"] in {
                "very_relaxed",
                "relaxed",
                "balanced",
                "strict",
                "very_strict",
            }
        else:
            assert 1 <= int(values["QUALITY_EVALUATION_MIN_SCORE"]) <= 5
        assert int(values["QUALITY_EVALUATION_MIN_SOURCES"]) >= 0
        _assert_dashscope_base_url(values["QUALITY_EVALUATION_BASE_URL"])

    def test_configuration_and_model_factory_use_dashscope_settings(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        values = _dashscope_test_env()
        captured: dict[str, object] = {}

        class FakeModel:
            def with_config(self, kwargs):
                captured["init"] = kwargs
                return self

            def bind(self, **kwargs):
                captured["bind"] = kwargs
                return self

        for key, value in values.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("QUALITY_EVALUATION_API_KEY", raising=False)
        monkeypatch.setattr(
            "open_deep_research.models.resolution.get_configurable_model_template",
            lambda: FakeModel(),
        )

        configurable = Configuration.from_runnable_config({"configurable": {}})
        _build_quality_model(configurable, {"configurable": {}})

        model_spec = values["QUALITY_EVALUATION_MODEL"]
        assert configurable.quality_evaluation_model == model_spec
        assert (
            configurable.quality_evaluation_base_url
            == values["QUALITY_EVALUATION_BASE_URL"]
        )
        expected_init = {
            "model": model_spec,
            "max_tokens": configurable.quality_evaluation_model_max_tokens,
            "max_retries": 0,
            "api_key": values["DASHSCOPE_API_KEY"],
            "base_url": values["QUALITY_EVALUATION_BASE_URL"],
            "metadata": {"sandbox_model_role": "quality_evaluation"},
        }
        if model_spec.split(":", 1)[1].lower().startswith("qwen"):
            expected_init["extra_body"] = {
                "enable_thinking": dashscope_qwen_enable_thinking(model_spec)
            }
        assert captured["init"] == expected_init
        assert captured["bind"] == {"response_format": {"type": "json_object"}}

    @pytest.mark.skipif(
        os.getenv(LIVE_TEST_ENV) != "1",
        reason=f"Set {LIVE_TEST_ENV}=1 to call the configured DashScope endpoint",
    )
    def test_live_openai_compatible_json_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        values = _install_live_quality_env(monkeypatch)
        _assert_dashscope_base_url(values["QUALITY_EVALUATION_BASE_URL"])
        model = values["QUALITY_EVALUATION_MODEL"].split(":", 1)[1]
        client = OpenAI(
            api_key=values["DASHSCOPE_API_KEY"],
            base_url=values["QUALITY_EVALUATION_BASE_URL"],
            timeout=30.0,
            max_retries=0,
        )

        response = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": 'Return exactly this JSON object: {"ok": true}',
            }],
            response_format={"type": "json_object"},
            extra_body={
                "enable_thinking": dashscope_qwen_enable_thinking(model)
            },
        )

        content = response.choices[0].message.content
        assert content is not None
        assert json.loads(content) == {"ok": True}

    @pytest.mark.skipif(
        os.getenv(LIVE_TEST_ENV) != "1",
        reason=f"Set {LIVE_TEST_ENV}=1 to probe DashScope forced tool output",
    )
    def test_live_openai_compatible_forced_tool_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-thinking mode must yield one well-formed structured call."""
        values = _install_live_quality_env(monkeypatch)
        model = values["QUALITY_EVALUATION_MODEL"].split(":", 1)[1]
        client = OpenAI(
            api_key=values["DASHSCOPE_API_KEY"],
            base_url=values["QUALITY_EVALUATION_BASE_URL"],
            max_retries=0,
            timeout=180,
        )

        response = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": "Return ok=true using the required function.",
            }],
            tools=[structured_output_tool(_LiveJudgeProbe, strict=False)],
            tool_choice=structured_tool_choice(),
            max_completion_tokens=4096,
            temperature=0,
            extra_body={"enable_thinking": False},
        )

        calls = response.choices[0].message.tool_calls or []
        assert len(calls) == 1
        assert calls[0].function.name == STRUCTURED_OUTPUT_TOOL_NAME
        assert json.loads(calls[0].function.arguments) == {"ok": True}

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.getenv(LIVE_TEST_ENV) != "1",
        reason=f"Set {LIVE_TEST_ENV}=1 to call the runtime quality gate",
    )
    async def test_live_runtime_quality_gate_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        values = _install_live_quality_env(monkeypatch)
        _assert_dashscope_base_url(values["QUALITY_EVALUATION_BASE_URL"])

        result = await evaluate_tool_results(
            "Assess whether three official sources support a test claim.",
            [{
                "name": "tavily_search",
                "content": (
                    "Source A: https://example.com/official-a supports the claim. "
                    "Source B: https://example.org/official-b independently supports it. "
                    "Source C: https://example.net/official-c confirms it."
                ),
                "error": False,
            }],
            {
                "configurable": {},
                "metadata": {"run_id": "dashscope-quality-live-test"},
            },
        )

        assert result.evaluator_error is None
        assert result.deterministic_checks["passed"] is True
        assert result.deterministic_checks["source_count"] == 3
        assert 1 <= result.relevance <= 5
        assert 1 <= result.source_quality <= 5
        assert 1 <= result.evidence_coverage <= 5
        assert 1 <= result.corroboration <= 5

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.getenv(LIVE_TEST_ENV) != "1",
        reason=f"Set {LIVE_TEST_ENV}=1 to run the quality-gate lifecycle",
    )
    async def test_live_quality_gate_converges_after_targeted_evidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exercise weak evidence, remediation, and final v4 admission."""
        _install_live_quality_env(monkeypatch)
        research_topic = (
            "核验 Python 3.13 free-threaded CPython 的边界："
            "说明它是否属于可选且实验性的独立构建；"
            "说明如何判断 GIL 是否启用，以及不兼容扩展对 GIL 的影响；"
            "不得把它表述为 Python 3.13 默认构建已经移除 GIL。"
        )
        contract = build_research_coverage_contract([
            HumanMessage(content=research_topic),
        ])
        requirement_ids = list(contract.delegable_requirement_ids())
        assert requirement_ids, "live scenario must compile factual requirements"
        evidence = _python_free_threading_evidence(requirement_ids)
        config = _live_gate_config()

        weak_tool_results, weak_evidence = await _prepare_governed_web_batch(
            evidence[:1],
            config,
        )
        started = time.perf_counter()
        weak = await evaluate_tool_results(
            research_topic,
            weak_tool_results,
            config,
            evidence_registry=weak_evidence,
            coverage_contract=contract,
            requirement_ids=requirement_ids,
        )
        _emit_live_observation(
            "tool_gate_before_remediation",
            weak,
            elapsed_seconds=time.perf_counter() - started,
        )

        assert weak.evaluator_error is None
        assert weak.protocol_errors == []
        assert weak.deterministic_checks["source_count"] == 1
        assert weak.decision == "retry"
        assert weak.missing_information or weak.suggested_queries, (
            "a rejected tool batch must provide actionable remediation"
        )

        strong_tool_results, registered_evidence = (
            await _prepare_governed_web_batch(evidence, config)
        )
        started = time.perf_counter()
        strong = await evaluate_tool_results(
            research_topic,
            strong_tool_results,
            config,
            evidence_registry=registered_evidence,
            coverage_contract=contract,
            requirement_ids=requirement_ids,
        )
        _emit_live_observation(
            "tool_gate_after_remediation",
            strong,
            elapsed_seconds=time.perf_counter() - started,
        )

        assert strong.evaluator_error is None
        assert strong.protocol_errors == []
        assert strong.deterministic_checks["passed"] is True
        assert strong.decision == "complete", (
            "quality gate did not converge after every stated gap received "
            f"primary-source evidence: {strong.model_dump(mode='json')}"
        )

        handoff = {
            "compressed_research": (
                "Python 3.13 的 free-threaded CPython 是可选且仍属实验性的独立"
                "构建形态，并不表示默认 CPython 已移除 GIL。PEP 703 将"
                " --disable-gil 定义为构建期选项 [ev-pep703-build]。Python 3.13"
                " 的 What’s New 将该能力描述为实验性支持，并通过单独构建提供"
                " [ev-py313-experimental]。运行时可以使用 sys._is_gil_enabled()"
                " 判断 GIL 状态；导入尚未声明支持 free-threading 的扩展模块时，"
                "解释器可能重新启用 GIL [ev-py313-runtime-gil]。因此，准确边界"
                "是‘可选实验构建允许无 GIL 运行’，而不是‘Python 3.13 默认"
                "已经无 GIL’。三个结论分别由 PEP、版本说明和运行 HOWTO 支撑。"
            ),
            "raw_notes": [strong_tool_results[0]["content"]],
            "evidence_registry": registered_evidence,
            "completion_reason": "research_complete",
        }
        started = time.perf_counter()
        handoff_result = await evaluate_subagent_handoff(
            research_topic,
            handoff,
            config,
            coverage_contract=contract,
            requirement_ids=requirement_ids,
        )
        _emit_live_observation(
            "handoff_gate_after_remediation",
            handoff_result,
            elapsed_seconds=time.perf_counter() - started,
        )

        assert handoff_result.evaluator_error is None
        assert handoff_result.protocol_errors == []
        assert handoff_result.deterministic_checks["passed"] is True
        assert handoff_result.accepted is True, (
            "fully grounded handoff was rejected after successful targeted "
            f"remediation: {handoff_result.model_dump(mode='json')}"
        )
        covered_ids = {
            item.requirement_id
            for item in handoff_result.requirement_coverage
            if item.status.value == "supported"
        }
        assert set(requirement_ids) <= covered_ids

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.getenv(LIVE_TEST_ENV) != "1",
        reason=f"Set {LIVE_TEST_ENV}=1 to run the bounded-retry probe",
    )
    async def test_live_repeated_gap_stops_at_research_iteration_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A persistent quality retry must route to compression at the hard cap."""
        _install_live_quality_env(monkeypatch)
        research_topic = (
            "核验 Python 3.13 free-threaded CPython 是否为实验性可选构建，"
            "并说明运行时 GIL 状态与不兼容扩展的影响。"
        )
        contract = build_research_coverage_contract([
            HumanMessage(content=research_topic),
        ])
        requirement_ids = list(contract.delegable_requirement_ids())
        evidence = _python_free_threading_evidence(requirement_ids)
        config = _live_gate_config()
        configurable = config["configurable"]
        assert isinstance(configurable, dict)
        configurable["max_react_tool_calls"] = 1
        pending, registered = await _prepare_governed_web_batch(
            evidence[:1],
            config,
        )
        state = {
            "research_topic": research_topic,
            "tool_call_iterations": 1,
            "pending_tool_results": pending,
            "research_complete_requested": False,
            "evidence_registry": registered,
            "coverage_contract": contract.model_dump(mode="json"),
            "requirement_ids": requirement_ids,
            "web_research_iterations": [],
        }

        started = time.perf_counter()
        command = await assess_research_results(state, config)
        assessment = ToolResultAssessment.model_validate(
            command.update["result_assessment"]
        )
        _emit_live_observation(
            "researcher_retry_at_iteration_limit",
            assessment,
            elapsed_seconds=time.perf_counter() - started,
        )

        assert assessment.evaluator_error is None
        assert assessment.decision == "retry"
        assert command.goto == "compress_research", (
            "a persistent quality gap must terminate research spending at "
            "max_react_tool_calls"
        )

    @pytest.mark.skipif(
        os.getenv(LIVE_TEST_ENV) != "1",
        reason=f"Set {LIVE_TEST_ENV}=1 to call the configured Judge endpoint",
    )
    def test_live_offline_judge_reuses_runtime_qwen_configuration(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        values = _install_live_quality_env(monkeypatch)
        for key in ("EVALUATION_MODEL", "EVALUATION_BASE_URL", "EVALUATION_API_KEY"):
            monkeypatch.delenv(key, raising=False)

        config = JudgeConfig.from_env()
        config = replace(
            config,
            api_key=values["DASHSCOPE_API_KEY"],
            base_url=values["QUALITY_EVALUATION_BASE_URL"],
            # Direct OpenAI-compatible endpoints receive the bare model id.
            model=config.model.split(":", 1)[-1],
        )
        probe = invoke_judge_structured_sync(
            _LiveJudgeProbe,
            [
                {
                    "role": "user",
                    "content": (
                        "Return ok=true to confirm this synthetic evaluation probe."
                    ),
                }
            ],
            operation="quality_live_probe",
            config=config,
        )

        assert probe.ok is True


class TestQualityEvaluationProviderIsolation:
    """Ensure one provider's credentials and request options do not leak to another."""

    def test_openai_model_uses_openai_key_when_dashscope_key_is_also_present(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        class FakeModel:
            def with_config(self, kwargs):
                captured["init"] = kwargs
                return self

            def bind(self, **kwargs):
                captured["bind"] = kwargs
                return self

        monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        monkeypatch.setattr(
            "open_deep_research.models.resolution.get_configurable_model_template",
            lambda: FakeModel(),
        )
        configurable = Configuration(
            quality_evaluation_model="openai:gpt-4.1-mini",
            quality_evaluation_base_url=None,
        )

        _build_quality_model(configurable, {"configurable": {}})

        assert captured["init"]["api_key"] == "openai-key"
        assert "extra_body" not in captured["init"]

    def test_native_anthropic_model_has_no_openai_only_options(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        class FakeModel:
            def with_config(self, kwargs):
                captured["init"] = kwargs
                return self

            def bind(self, **kwargs):
                captured["bind"] = kwargs
                return self

        monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
        monkeypatch.setattr(
            "open_deep_research.models.resolution.get_configurable_model_template",
            lambda: FakeModel(),
        )
        configurable = Configuration(
            quality_evaluation_model="anthropic:claude-sonnet-4-5",
            quality_evaluation_base_url=None,
        )

        _build_quality_model(configurable, {"configurable": {}})

        assert captured["init"]["api_key"] == "anthropic-key"
        assert "extra_body" not in captured["init"]
        assert "bind" not in captured

    def test_explicit_quality_key_supports_other_openai_compatible_endpoints(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        class FakeModel:
            def with_config(self, kwargs):
                captured.update(kwargs)
                return self

            def bind(self, **_kwargs):
                return self

        monkeypatch.setenv("QUALITY_EVALUATION_API_KEY", "endpoint-key")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        monkeypatch.setattr(
            "open_deep_research.models.resolution.get_configurable_model_template",
            lambda: FakeModel(),
        )
        configurable = Configuration(
            quality_evaluation_model="openai:custom-model",
            quality_evaluation_base_url="https://models.example.test/v1",
        )

        _build_quality_model(configurable, {"configurable": {}})

        assert captured["api_key"] == "endpoint-key"

    def test_text_content_blocks_are_normalized_for_json_validation(self) -> None:
        assert _content_text([
            {"type": "text", "text": '{"decision":"continue"}'},
        ]) == '{"decision":"continue"}'

    def test_fenced_json_is_normalized_for_provider_compatibility(self) -> None:
        assert _content_text(
            '```json\n{"decision":"continue"}\n```',
        ) == '{"decision":"continue"}'

    @pytest.mark.asyncio
    async def test_handoff_judge_receives_authoritative_runtime_date(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        async def fake_evaluate_json(
            _schema,
            system_prompt,
            payload,
            _config,
            **_kwargs,
        ):
            captured["system_prompt"] = system_prompt
            captured["payload"] = payload
            return {
                "accepted": True,
                "relevance": 3,
                "source_quality": 3,
                "evidence_coverage": 3,
                "groundedness": 3,
                "missing_information": [],
                "unsupported_claims": [],
                "follow_up_tasks": [],
                "reason": "Synthetic acceptance.",
            }

        monkeypatch.setattr(
            "open_deep_research.quality.gate._evaluate_json",
            fake_evaluate_json,
        )
        handoff = {
            "compressed_research": (
                "A sufficiently detailed synthetic handoff supported by "
                "https://example.com/source-a and https://example.org/source-b. "
            )
            * 3,
            "raw_notes": [],
        }

        result = await evaluate_subagent_handoff(
            "Evaluate a synthetic research handoff.",
            handoff,
            {"configurable": {}},
        )

        assert result.accepted is True
        assert captured["payload"]["runtime_current_date"] == date.today().isoformat()
        assert isinstance(captured["payload"]["evidence_registry"], list)
        assert "training cutoff" in captured["system_prompt"]

    @pytest.mark.asyncio
    async def test_tool_gate_receives_deduplicated_json_native_evidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        async def fake_evaluate_json(
            _schema,
            _system_prompt,
            payload,
            _config,
            **_kwargs,
        ):
            captured["payload"] = payload
            return {
                "decision": "complete",
                "relevance": 4,
                "source_quality": 4,
                "evidence_coverage": 4,
                "corroboration": 4,
                "unresolved_conflicts": [],
                "missing_information": [],
                "suggested_queries": [],
                "reason": "The structured cumulative evidence is sufficient.",
            }

        monkeypatch.setattr(
            "open_deep_research.quality.gate._evaluate_json",
            fake_evaluate_json,
        )
        evidence_registry = [
            {
                "evidence_id": "ev-79",
                "claim": "PEP 8 limits code lines to 79 characters.",
                "supporting_excerpt": "Limit all lines to a maximum of 79 characters.",
                "source_url": "https://peps.python.org/pep-0008/",
                "source_authority": 1.0,
                "security_status": "accepted",
            },
            {
                "evidence_id": "ev-79",
                "claim": "Duplicate of the same evidence.",
                "source_url": "https://peps.python.org/pep-0008/",
                "security_status": "accepted",
            },
            {
                "evidence_id": "ev-72",
                "claim": "PEP 8 limits comments and docstrings to 72 characters.",
                "supporting_excerpt": (
                    "For flowing long blocks of text (docstrings or comments), "
                    "the line length should be limited to 72 characters."
                ),
                "source_url": "https://peps.python.org/pep-0008/",
                "source_authority": 1.0,
                "security_status": "accepted",
            },
        ]

        result = await evaluate_tool_results(
            "Extract both the 79- and 72-character PEP 8 recommendations.",
            [{
                "name": "fetch_url",
                "content": "Structured fetch completed.",
                "error": False,
            }],
            {
                "configurable": {
                    "quality_evaluation_rigor": "balanced",
                    "quality_evaluation_min_sources": 1,
                }
            },
            evidence_registry=evidence_registry,
        )

        payload = captured["payload"]
        assert isinstance(payload, dict)
        cumulative = payload["cumulative_evidence"]
        assert isinstance(cumulative, list)
        assert [item["evidence_id"] for item in cumulative] == ["ev-79", "ev-72"]
        assert "72 characters" in cumulative[1]["supporting_excerpt"]
        assert payload["cumulative_evidence_stats"] == {
            "accepted_count": 3,
            "unique_count": 2,
            "included_count": 2,
            "truncated": False,
        }
        assert result.decision == "complete"
