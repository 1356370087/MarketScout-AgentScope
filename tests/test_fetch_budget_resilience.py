"""Regression coverage for sandbox fetch-budget propagation and compensation."""

import asyncio
from types import SimpleNamespace

import pytest

from open_deep_research.configuration import Configuration
from open_deep_research.sandbox.gateway import (
    GatewayRunContext,
    GatewayRuntime,
    approval_deadline,
)
from open_deep_research.sandbox.wire import GatewayToolRequestV1
from open_deep_research.tools.base import ToolEffect, ToolExecutionZone
from open_deep_research.tools.web_research import definition as web_definition
from open_deep_research.tools.web_research import pipeline as web_pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("nested_model", [False, True])
async def test_gateway_tool_execution_uses_request_run_and_task_metadata(
    monkeypatch, tmp_path, nested_model,
) -> None:
    """The physical Gateway tool call must not collapse budget keys to defaults."""
    from open_deep_research.sandbox import gateway as gateway_module
    from open_deep_research.tools import governance, registry

    configurable = Configuration()
    runtime = GatewayRuntime(
        Configuration(
            sandbox_root_signing_key="a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        )
    )
    context = GatewayRunContext(
        config={
            "configurable": configurable.model_dump(mode="json"),
            "metadata": {"sandbox_gateway_physical": True},
        },
        fence_token=7,
        expires_at=9_999_999_999,
    )
    tool = SimpleNamespace(
        name="web_research",
        execution_zone=ToolExecutionZone.GATEWAY,
        effect=ToolEffect.READ_ONLY,
        egress_urls=lambda _arguments: [],
    )
    captured: dict = {}
    if nested_model:
        import httpx
        from openai import AsyncOpenAI

        from open_deep_research.models import gateway, invocation

        monkeypatch.delenv("LITELLM_SERVICE_KEY", raising=False)
        monkeypatch.setenv("RUNS_DIR", str(tmp_path))
        context.api_keys["LITELLM_RUN_KEY"] = "test-run-key"
        context.config["configurable"]["model_backend"] = "litellm"

        def respond(request):
            import json

            assert request.headers["authorization"] == "Bearer test-run-key"
            body = json.loads(request.content)
            parameters = body["tools"][0]["function"]["parameters"]
            arguments = '{"scores": []}' if "scores" in parameters["properties"] else '{"items": []}'
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0, "model": "test",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "call", "type": "function", "function": {
                            "name": "__insightforge_structured_output", "arguments": arguments,
                        },
                    }],
                }}],
            })

        client = AsyncOpenAI(api_key="placeholder", base_url="http://test/v1",
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        model_gateway = gateway.LiteLLMModelGateway(base_url="http://test/v1", client=client)
        monkeypatch.setattr(invocation, "get_model_gateway", lambda _: model_gateway)

    async def fake_assemble_toolset(_role, _config):
        return [tool]

    async def fake_execute(
        _call,
        _tools,
        _role,
        execution_config,
        *,
        operation_id,
    ):
        del operation_id
        captured["metadata"] = dict(execution_config.get("metadata") or {})
        if nested_model:
            assert await web_pipeline._rerank_web_candidates("topic", [], execution_config) == {}
            from open_deep_research.web.models import DocumentChunk

            chunk = DocumentChunk(chunk_id="c", document_id="d", text="A complete factual sentence.", content_hash="h")
            assert await web_pipeline._extract_web_evidence(
                "topic", {"d": SimpleNamespace(title="source")}, [chunk], execution_config,
            ) == []
        return SimpleNamespace(
            error=None,
            result=SimpleNamespace(output={"ok": True}),
            message=SimpleNamespace(content=""),
        )

    async def fake_post(_path, _request):
        return {}

    profile = SimpleNamespace(
        approval_policy="never",
        resources=SimpleNamespace(approval_timeout_seconds=60),
        network=SimpleNamespace(),
    )
    monkeypatch.setattr(registry, "assemble_toolset", fake_assemble_toolset)
    monkeypatch.setattr(governance, "execute_governed_tool_call", fake_execute)
    monkeypatch.setattr(
        gateway_module,
        "resolve_profile",
        lambda _configuration: (SimpleNamespace(), "research-gateway-only", profile),
    )
    monkeypatch.setattr(
        gateway_module,
        "tool_policy_decision",
        lambda *_args, **_kwargs: "allow",
    )
    monkeypatch.setattr(runtime.internal, "post", fake_post)
    request = GatewayToolRequestV1(
        run_id="run-rpc-metadata",
        task_id="task-rpc-metadata",
        role="researcher",
        stage="researching",
        execution_zone="gateway",
        logical_operation_id="operation-rpc-metadata",
        tool_call_id="tool-call-rpc-metadata",
        tool_name="web_research",
        arguments={"objective": "topic", "queries": ["query"]},
    )

    try:
        outcome = await runtime.invoke_tool(request, context)
    finally:
        if nested_model:
            await client.close()

    assert outcome.status == "completed"
    assert captured["metadata"]["run_id"] == request.run_id
    assert captured["metadata"]["task_id"] == request.task_id


@pytest.mark.asyncio
async def test_web_research_releases_fetch_reservation_when_runner_raises(
    monkeypatch,
) -> None:
    """A failed pipeline call must return every unconsumed reserved fetch slot."""
    monkeypatch.setenv("FETCH_TOP_K", "3")
    monkeypatch.setenv("MAX_FETCHES_PER_RESEARCHER", "3")
    monkeypatch.setenv("MAX_FETCHES_PER_RUN", "3")
    # Single-unit pool: these tests exercise release semantics, and fair
    # share would otherwise hold back floors for unseen sibling tasks.
    monkeypatch.setenv("MAX_CONCURRENT_RESEARCH_UNITS", "1")
    config = {
        "configurable": {},
        "metadata": {"run_id": "run-release", "task_id": "task-release"},
    }
    web_pipeline.clear_run_web_budget("run-release")

    async def fail_run(
        _self,
        _request,
        *,
        remaining_fetches=None,
        on_physical_fetch=None,
        fetch_budget_exhaustion_scope="none",
        run_id=None,
        fetch_budget_exhaustion_cause="attempts",
    ):
        assert remaining_fetches == 3
        assert on_physical_fetch is not None
        assert fetch_budget_exhaustion_scope == "none"
        assert run_id == "run-release"
        raise RuntimeError("injected pipeline failure")

    monkeypatch.setattr(web_definition.WebResearchPipeline, "run", fail_run)
    try:
        with pytest.raises(RuntimeError, match="injected pipeline failure"):
            await web_definition._web_research_call.coroutine(
                objective="topic",
                queries=["query"],
                config=config,
            )

        reservation = await web_pipeline._reserve_fetch_budget(config, 3)
        assert reservation.reserved == 3
    finally:
        web_pipeline.clear_run_web_budget("run-release")


@pytest.mark.asyncio
async def test_web_research_releases_only_unused_slots_after_partial_failure(
    monkeypatch,
) -> None:
    """Physical attempts remain charged when a later pipeline stage raises."""
    monkeypatch.setenv("FETCH_TOP_K", "3")
    monkeypatch.setenv("MAX_FETCHES_PER_RESEARCHER", "3")
    monkeypatch.setenv("MAX_FETCHES_PER_RUN", "3")
    # Single-unit pool: these tests exercise release semantics, and fair
    # share would otherwise hold back floors for unseen sibling tasks.
    monkeypatch.setenv("MAX_CONCURRENT_RESEARCH_UNITS", "1")
    config = {
        "configurable": {},
        "metadata": {"run_id": "run-partial", "task_id": "task-partial"},
    }
    web_pipeline.clear_run_web_budget("run-partial")

    async def fail_after_two_fetches(
        _self,
        _request,
        *,
        remaining_fetches=None,
        on_physical_fetch=None,
        fetch_budget_exhaustion_scope="none",
        run_id=None,
        fetch_budget_exhaustion_cause="attempts",
    ):
        assert remaining_fetches == 3
        assert on_physical_fetch is not None
        assert fetch_budget_exhaustion_scope == "none"
        assert run_id == "run-partial"
        on_physical_fetch()
        on_physical_fetch()
        raise RuntimeError("injected post-fetch failure")

    monkeypatch.setattr(
        web_definition.WebResearchPipeline,
        "run",
        fail_after_two_fetches,
    )
    try:
        with pytest.raises(RuntimeError, match="injected post-fetch failure"):
            await web_definition._web_research_call.coroutine(
                objective="topic",
                queries=["query"],
                config=config,
            )

        reservation = await web_pipeline._reserve_fetch_budget(config, 3)
        assert reservation.reserved == 1
    finally:
        web_pipeline.clear_run_web_budget("run-partial")


@pytest.mark.asyncio
async def test_gateway_terminal_cleanup_releases_run_fetch_counters() -> None:
    """Unregister/TTL cleanup must not retain process-local run budget keys."""
    run_id = "run-terminal-cleanup"
    config = {
        "configurable": {
            "max_fetches_per_researcher": 2,
            "max_fetches_per_run": 2,
            "max_concurrent_research_units": 1,
        },
        "metadata": {"run_id": run_id, "task_id": "task-cleanup"},
    }
    web_pipeline.clear_run_web_budget(run_id)
    runtime = GatewayRuntime(
        Configuration(
            sandbox_root_signing_key="a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        )
    )
    runtime.runs[run_id] = GatewayRunContext(
        config={"configurable": {}, "metadata": {}},
        fence_token=1,
        expires_at=9_999_999_999,
    )
    try:
        reservation = await web_pipeline._reserve_fetch_budget(config, 2)
        assert reservation.reserved == 2

        runtime._remove_run_context(run_id, clear_fetch_budget=True)

        reservation = await web_pipeline._reserve_fetch_budget(config, 2)
        assert reservation.reserved == 2
    finally:
        web_pipeline.clear_run_web_budget(run_id)


def test_followup_wave_detection() -> None:
    assert web_pipeline._is_followup_wave("wave-0") is False  # noqa: SLF001
    assert web_pipeline._is_followup_wave("wave-1") is True  # noqa: SLF001
    assert web_pipeline._is_followup_wave("wave-2") is True  # noqa: SLF001
    assert web_pipeline._is_followup_wave("") is False  # noqa: SLF001
    assert web_pipeline._is_followup_wave("wave-x") is False  # noqa: SLF001


def test_each_approval_gets_a_fresh_window(monkeypatch) -> None:
    context = GatewayRunContext(
        config={"configurable": {}, "metadata": {}},
        fence_token=1,
        expires_at=10_000,
        registered_at=100,
    )
    monkeypatch.setattr("open_deep_research.sandbox.gateway.time.time", lambda: 500)
    assert approval_deadline(context, timeout_seconds=60) == 560

    monkeypatch.setattr("open_deep_research.sandbox.gateway.time.time", lambda: 700)
    assert approval_deadline(context, timeout_seconds=60) == 760


@pytest.mark.asyncio
async def test_first_wave_respects_headroom_and_followup_draws_it() -> None:
    run_id = "run-headroom"
    web_pipeline.clear_run_web_budget(run_id)
    config = {
        "configurable": {
            "max_fetches_per_researcher": 12,
            "max_fetches_per_run": 10,
        },
        # No research_wave_id: conservative first-wave classification.
        "metadata": {"run_id": run_id, "task_id": "task-a"},
    }

    try:
        # Auto reserve = min(12, 10 // 5) = 2 → first-wave cap is 8. Fair
        # share (concurrency 5, floor 1) holds back one slot per unseen
        # sibling task, so the first task draws the 4-slot slack first.
        first = await web_pipeline._reserve_fetch_budget(config, 12)  # noqa: SLF001
        assert first.reserved == 4
        assert first.exhaustion_scope == "none"

        # The task already consumed more than its cumulative floor; the
        # remaining first-wave slots stay reserved for its unseen siblings.
        again = await web_pipeline._reserve_fetch_budget(config, 5)  # noqa: SLF001
        assert again.reserved == 0
        assert again.exhaustion_scope == "task"

        followup = await web_pipeline._reserve_fetch_budget(
            {
                **config,
                "metadata": {
                    **config["metadata"],
                    "task_id": "task-followup",
                    "research_wave_id": "wave-2",
                },
            },
            5,
        )
        assert followup.reserved == 2
        assert followup.exhaustion_scope == "none"
    finally:
        web_pipeline.clear_run_web_budget(run_id)


@pytest.mark.asyncio
async def test_explicit_zero_reserve_disables_headroom() -> None:
    run_id = "run-no-headroom"
    web_pipeline.clear_run_web_budget(run_id)
    config = {
        "configurable": {
            "max_fetches_per_researcher": 12,
            "max_fetches_per_run": 10,
            "fetch_budget_followup_reserve": 0,
        },
        "metadata": {"run_id": run_id, "task_id": "task-a"},
    }

    try:
        # Fair share: floor = 10 // 5 = 2, unseen = 4 → slack = 10 - 8 = 2.
        first = await web_pipeline._reserve_fetch_budget(config, 12)  # noqa: SLF001
        assert first.reserved == 2
    finally:
        web_pipeline.clear_run_web_budget(run_id)


@pytest.mark.asyncio
async def test_late_sibling_keeps_a_fair_share_floor() -> None:
    """A task arriving after four siblings still receives its floor.

    E2E round 5: first-come-first-served let four siblings drain the wave
    pool, and the EU task saw exhausted_scope="run" from its first reserve.
    """
    run_id = "run-fair-share"
    web_pipeline.clear_run_web_budget(run_id)
    base = {
        "configurable": {
            "max_fetches_per_researcher": 12,
            "max_fetches_per_run": 40,
        },
        "metadata": {"run_id": run_id, "task_id": ""},
    }

    try:
        # Auto headroom = min(12, 40 // 5) = 8 → first-wave cap 32, floor 6.
        first_config = {
            **base,
            "metadata": {**base["metadata"], "task_id": "early-0"},
        }
        first = await web_pipeline._reserve_fetch_budget(first_config, 12)
        assert first.reserved == 8
        repeated = await web_pipeline._reserve_fetch_budget(first_config, 12)
        assert repeated.reserved == 0
        assert repeated.exhaustion_scope == "task"

        for index in range(1, 4):
            config = {
                **base,
                "metadata": {
                    **base["metadata"],
                    "task_id": f"early-{index}",
                },
            }
            early = await web_pipeline._reserve_fetch_budget(config, 12)
            assert early.reserved >= 1
            assert early.exhaustion_scope == "none"
        late_config = {
            **base,
            "metadata": {**base["metadata"], "task_id": "late-eu"},
        }
        late = await web_pipeline._reserve_fetch_budget(late_config, 5)
        assert late.reserved == 5
        assert late.exhaustion_scope == "none"
        assert web_pipeline._WEB_RUN_FETCH_ATTEMPTS[run_id] <= 32  # noqa: SLF001
    finally:
        web_pipeline.clear_run_web_budget(run_id)


@pytest.mark.asyncio
async def test_parallel_zero_allocations_emit_only_two_iterations() -> None:
    run_id = "run-zero-allocation-cap"
    config = {
        "configurable": {
            "max_fetches_per_researcher": 1,
            "max_fetches_per_run": 1,
            "fetch_budget_followup_reserve": 0,
        },
        "metadata": {
            "run_id": run_id,
            "task_id": "task-zero-allocation-cap",
        },
    }
    web_pipeline.clear_run_web_budget(run_id)

    try:
        consumed = await web_pipeline._reserve_fetch_budget(config, 1)  # noqa: SLF001
        assert consumed.reserved == 1

        denied = await asyncio.gather(*[
            web_pipeline._reserve_fetch_budget(config, 1)  # noqa: SLF001
            for _index in range(4)
        ])

        assert [item.emit_exhaustion_iteration for item in denied] == [
            True,
            True,
            False,
            False,
        ]
    finally:
        web_pipeline.clear_run_web_budget(run_id)


@pytest.mark.asyncio
async def test_physical_fetch_resets_zero_allocation_iteration_cap() -> None:
    run_id = "run-zero-allocation-reset"
    config = {
        "configurable": {
            "max_fetches_per_researcher": 1,
            "max_fetches_per_run": 1,
            "fetch_budget_followup_reserve": 0,
        },
        "metadata": {
            "run_id": run_id,
            "task_id": "task-zero-allocation-reset",
        },
    }
    web_pipeline.clear_run_web_budget(run_id)

    try:
        consumed = await web_pipeline._reserve_fetch_budget(config, 1)  # noqa: SLF001
        assert consumed.reserved == 1
        first = await web_pipeline._reserve_fetch_budget(config, 1)  # noqa: SLF001
        second = await web_pipeline._reserve_fetch_budget(config, 1)  # noqa: SLF001
        suppressed = await web_pipeline._reserve_fetch_budget(config, 1)  # noqa: SLF001
        assert first.emit_exhaustion_iteration is True
        assert second.emit_exhaustion_iteration is True
        assert suppressed.emit_exhaustion_iteration is False

        web_pipeline._record_physical_fetch(config)  # noqa: SLF001

        reset = await web_pipeline._reserve_fetch_budget(config, 1)  # noqa: SLF001
        assert reset.emit_exhaustion_iteration is True
    finally:
        web_pipeline.clear_run_web_budget(run_id)


@pytest.mark.asyncio
async def test_transport_failures_refund_attempt_budget_and_charge_allowance(
    monkeypatch,
) -> None:
    """超时/连接类失败退还抓取份额，计入有界失败额度而非永久消耗。"""
    monkeypatch.setenv("FETCH_TOP_K", "5")
    monkeypatch.setenv("MAX_FETCHES_PER_RESEARCHER", "5")
    monkeypatch.setenv("MAX_FETCHES_PER_RUN", "5")
    monkeypatch.setenv("MAX_CONCURRENT_RESEARCH_UNITS", "1")
    config = {
        "configurable": {},
        "metadata": {"run_id": "run-transport", "task_id": "task-transport"},
    }
    web_pipeline.clear_run_web_budget("run-transport")

    from open_deep_research.web.models import BudgetSnapshot

    async def run_with_transport_failures(
        _self,
        _request,
        *,
        remaining_fetches=None,
        on_physical_fetch=None,
        fetch_budget_exhaustion_scope="none",
        run_id=None,
        fetch_budget_exhaustion_cause="attempts",
    ):
        for _ in range(3):
            on_physical_fetch()
        return SimpleNamespace(
            budget=BudgetSnapshot(
                fetch_attempts=3,
                transport_failed_fetches=2,
                reserved_fetches=remaining_fetches or 0,
            )
        )

    monkeypatch.setattr(
        web_definition.WebResearchPipeline,
        "run",
        run_with_transport_failures,
    )
    monkeypatch.setattr(
        web_definition.pipeline,
        "_record_web_pipeline_metrics",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        web_definition.pipeline,
        "_compact_web_result",
        lambda result, _config: "{}",
    )
    try:
        await web_definition._web_research_call.coroutine(
            objective="topic",
            queries=["query"],
            config=config,
        )
        task_key = ("run-transport", "task-transport")
        # 3 次物理尝试中 2 次传输失败被退还：净消耗 1。
        assert web_pipeline._WEB_TASK_FETCH_ATTEMPTS[task_key] == 1
        assert web_pipeline._WEB_RUN_FETCH_ATTEMPTS["run-transport"] == 1
        # 失败额度记账 2 次。
        assert web_pipeline._WEB_TASK_TRANSPORT_FAILURES[task_key] == 2
        assert web_pipeline._WEB_RUN_TRANSPORT_FAILURES["run-transport"] == 2
    finally:
        web_pipeline.clear_run_web_budget("run-transport")


@pytest.mark.asyncio
async def test_transport_failure_allowance_still_terminates_dead_environment(
    monkeypatch,
) -> None:
    """失败额度封顶后照常返回零配额墙，风暴防护仍在。"""
    monkeypatch.setenv("MAX_FETCHES_PER_RESEARCHER", "4")
    monkeypatch.setenv("MAX_FETCHES_PER_RUN", "4")
    monkeypatch.setenv("MAX_CONCURRENT_RESEARCH_UNITS", "1")
    config = {
        "configurable": {},
        "metadata": {"run_id": "run-dead", "task_id": "task-dead"},
    }
    web_pipeline.clear_run_web_budget("run-dead")
    try:
        web_pipeline._WEB_TASK_TRANSPORT_FAILURES[("run-dead", "task-dead")] = 4
        reservation = await web_pipeline._reserve_fetch_budget(config, 5)
        assert reservation.reserved == 0
        assert reservation.exhaustion_cause == "transport_failures"
        assert reservation.exhaustion_scope == "task"

        web_pipeline._WEB_RUN_TRANSPORT_FAILURES["run-dead"] = 4
        reservation = await web_pipeline._reserve_fetch_budget(config, 5)
        assert reservation.reserved == 0
        assert reservation.exhaustion_cause == "transport_failures"
        assert reservation.exhaustion_scope == "run_and_task"
    finally:
        web_pipeline.clear_run_web_budget("run-dead")


def test_budget_exhaustion_reason_names_the_actual_wall() -> None:
    from open_deep_research.web.models import BudgetSnapshot, SearchRequest
    from open_deep_research.web.pipeline import analyze_gaps

    request = SearchRequest(objective="obj", queries=["q"])
    attempts = analyze_gaps(
        request,
        [],
        [],
        BudgetSnapshot(exhausted=True, exhaustion_scope="task"),
    )
    assert "successful-document" not in attempts.reason
    assert "fetch-attempt fair share" in attempts.reason

    transport = analyze_gaps(
        request,
        [],
        [],
        BudgetSnapshot(
            exhausted=True,
            exhaustion_scope="task",
            exhaustion_cause="transport_failures",
        ),
    )
    assert "transport-failure allowance" in transport.reason
