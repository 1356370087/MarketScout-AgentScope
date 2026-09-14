"""Controller-side usage backfill for gateway-mediated model operations.

Worker and Gateway processes run without a trace store by design, so the
authenticated ``operations/transition`` endpoint is the single writer that
re-enters journaled outcomes into the usage projection (E2E round 8 gap:
28 usage rows versus 60 journaled gateway operations). Tool-execution model
calls in the Gateway are forwarded over ``/internal/sandbox/usage/report``,
and historical journals are healed by ``reconcile_run_gateway_usage``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from open_deep_research.configuration import Configuration
from open_deep_research.observability import (
    invoke_model_with_retry_observability,
)
from open_deep_research.observability.core import SQLiteTraceStore
from open_deep_research.sandbox import internal_api as internal_api_module
from open_deep_research.sandbox.crypto import SandboxDerivedKeys, sign_payload
from open_deep_research.sandbox.internal_api import (
    InternalRunContext,
    OperationTransitionRequest,
    UsageReportRequest,
    backfill_gateway_usage_event,
    build_internal_sandbox_router,
    reconcile_run_gateway_usage,
)
from open_deep_research.sandbox.operations import ModelOperationStore

_ROOT_KEY = base64.b64encode(b"backfill-test-root-key-32-bytes!!"[:32]).decode()


def _context(
    tmp_path,
    *,
    run_id: str = "backfill-run",
    accounting: bool = True,
    root_key: str = _ROOT_KEY,
) -> InternalRunContext:
    trace_path = tmp_path / "traces.sqlite3"
    configurable = Configuration(
        token_usage_accounting_enabled=accounting,
        observability_enabled=accounting,
        sqlite_observability_enabled=accounting,
        trace_store_path=str(trace_path),
        runs_dir=str(tmp_path / "runs"),
        event_log_enabled=False,
        sandbox_root_signing_key=root_key,
    )
    return InternalRunContext(
        config={
            "configurable": configurable.model_dump(mode="json"),
            "metadata": {"run_id": run_id},
        },
        configurable=configurable,
        fence_token=1,
        started_at=time.time(),
    )


def _record(
    *,
    run_id: str = "backfill-run",
    operation_id: str = "op-backfill-1",
    status: str = "completed",
    usage: dict[str, int] | None = None,
    role: str | None = "researcher",
    model: str | None = "openai:gpt-test",
    task_id: str = "task-1",
    stage: str = "researching",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task_id": task_id,
        "stage": stage,
        "logical_operation_id": operation_id,
        "physical_attempts": ["pa-1"],
        "status": status,
        "outcome": {
            "status": status,
            "usage": (
                usage
                if usage is not None
                else {"input_tokens": 11, "output_tokens": 7}
            ),
            "role": role,
            "model": model,
        },
    }


def _store(tmp_path) -> SQLiteTraceStore:
    return SQLiteTraceStore(str(tmp_path / "traces.sqlite3"))


def test_backfill_writes_provider_reported_row(tmp_path):
    context = _context(tmp_path)

    backfill_gateway_usage_event(context, _record())

    rows = _store(tmp_path)._usage_rows("backfill-run")  # noqa: SLF001
    assert len(rows) == 1
    row = rows[0]
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (11, 7, 18)
    assert row["usage_source"] == "provider_reported"
    assert row["response_status"] == "success"
    assert row["stage"] == "researching"
    assert row["task_id"] == "task-1"
    assert row["agent_role"] == "researcher"
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-test"
    assert row["event_key"] == "op-backfill-1"
    assert row["operation"] == "gateway.model"


def test_backfill_is_idempotent_per_logical_operation(tmp_path):
    context = _context(tmp_path)
    record = _record()

    backfill_gateway_usage_event(context, record)
    backfill_gateway_usage_event(context, record)

    assert len(_store(tmp_path)._usage_rows("backfill-run")) == 1  # noqa: SLF001


def test_backfill_maps_terminal_statuses_and_empty_usage(tmp_path):
    context = _context(tmp_path)

    backfill_gateway_usage_event(
        context,
        _record(operation_id="op-failed", status="failed", usage={"input_tokens": 3, "output_tokens": 0}),
    )
    backfill_gateway_usage_event(
        context,
        _record(operation_id="op-uncertain", status="uncertain", usage={"input_tokens": 5, "output_tokens": 2}),
    )
    backfill_gateway_usage_event(
        context,
        _record(operation_id="op-empty", status="completed", usage={}),
    )

    rows = {
        row["event_key"]: row
        for row in _store(tmp_path)._usage_rows("backfill-run")  # noqa: SLF001
    }
    assert rows["op-failed"]["response_status"] == "rejected"
    assert rows["op-uncertain"]["response_status"] == "unknown_failed"
    assert rows["op-empty"]["usage_source"] == "missing"
    assert rows["op-empty"]["input_tokens"] == 0


def test_backfill_marks_attempt_count_from_journal(tmp_path):
    context = _context(tmp_path)
    record = _record()
    record["physical_attempts"] = ["pa-1", "pa-2", "pa-3"]

    backfill_gateway_usage_event(context, record)

    rows = _store(tmp_path)._usage_rows("backfill-run")  # noqa: SLF001
    assert rows[0]["attempt_index"] == 3


def test_backfill_skips_when_trace_store_disabled(tmp_path):
    context = _context(tmp_path, accounting=False)

    backfill_gateway_usage_event(context, _record())

    assert _store(tmp_path)._usage_rows("backfill-run") == []  # noqa: SLF001


def test_backfill_survives_malformed_records(tmp_path):
    context = _context(tmp_path)

    backfill_gateway_usage_event(
        context, {"run_id": "backfill-run", "outcome": "not-a-dict"}
    )

    assert _store(tmp_path)._usage_rows("backfill-run") == []  # noqa: SLF001


def _signed(model_cls, **fields):
    unsigned = model_cls(
        service_timestamp=time.time(),
        service_nonce=secrets.token_urlsafe(24),
        service_signature="placeholder",
        **fields,
    )
    signature = sign_payload(
        unsigned.signed_payload(),
        SandboxDerivedKeys.from_root(_ROOT_KEY).service_auth,
    )
    return model_cls.model_validate(
        {**unsigned.model_dump(mode="json"), "service_signature": signature}
    )


def test_transition_endpoint_backfills_usage_projection(tmp_path):
    context = _context(tmp_path)
    app = FastAPI()
    app.include_router(build_internal_sandbox_router(lambda _run_id: context))
    store = ModelOperationStore(
        "backfill-run", runs_dir=str(tmp_path / "runs")
    )
    store.reserve(
        task_id="task-1",
        stage="finalizing",
        logical_operation_id="op-endpoint-1",
        physical_attempt_id="pa-1",
    )
    store.transition("op-endpoint-1", expected={"reserved"}, status="dispatched")

    outcome = {
        "status": "completed",
        "logical_operation_id": "op-endpoint-1",
        "physical_attempt_id": "pa-1",
        "usage": {"input_tokens": 9500, "output_tokens": 2500},
        "role": "quality_evaluation",
        "model": "openai:qwen-judge",
    }
    request = _signed(
        OperationTransitionRequest,
        run_id="backfill-run",
        fence_token=1,
        logical_operation_id="op-endpoint-1",
        status="completed",
        outcome=outcome,
    )

    with TestClient(app) as client:
        response = client.post(
            "/internal/sandbox/operations/transition",
            json=request.model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    rows = _store(tmp_path)._usage_rows("backfill-run")  # noqa: SLF001
    assert len(rows) == 1
    assert rows[0]["event_key"] == "op-endpoint-1"
    assert rows[0]["input_tokens"] == 9500
    assert rows[0]["agent_role"] == "quality_evaluation"
    assert rows[0]["stage"] == "finalizing"


class _GatewayLikeModel:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error

    def with_config(self, _config):
        return self

    async def ainvoke(self, _messages):
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_caller_side_usage_defers_to_gateway_marker(tmp_path):
    trace_path = tmp_path / "traces.sqlite3"
    config = {
        "configurable": {
            "trace_store_path": str(trace_path),
            "event_log_enabled": False,
        },
        "metadata": {"run_id": "defer-run"},
    }
    marker_response = AIMessage(
        content="gateway",
        usage_metadata={"input_tokens": 2, "output_tokens": 4, "total_tokens": 6},
        response_metadata={"gateway_logical_operation_id": "op-marker"},
    )
    local_response = AIMessage(
        content="local",
        usage_metadata={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
    )

    await invoke_model_with_retry_observability(
        _GatewayLikeModel(response=marker_response),
        [HumanMessage(content="q")],
        {**config, "metadata": {"run_id": "defer-run-gateway"}},
        span_name="gateway.researcher.model",
        agent_role="researcher",
        model_name="openai:gpt-test",
        max_attempts=1,
    )
    await invoke_model_with_retry_observability(
        _GatewayLikeModel(response=local_response),
        [HumanMessage(content="q")],
        {**config, "metadata": {"run_id": "defer-run-local"}},
        span_name="researcher.model",
        agent_role="researcher",
        model_name="openai:gpt-test",
        max_attempts=1,
    )

    store = SQLiteTraceStore(str(trace_path))
    assert store._usage_rows("defer-run-gateway") == []  # noqa: SLF001
    local_rows = store._usage_rows("defer-run-local")  # noqa: SLF001
    assert len(local_rows) == 1
    assert local_rows[0]["input_tokens"] == 3


@pytest.mark.asyncio
async def test_caller_side_failure_defers_to_gateway_marker(tmp_path):
    trace_path = tmp_path / "traces.sqlite3"
    config = {
        "configurable": {
            "trace_store_path": str(trace_path),
            "event_log_enabled": False,
        },
        "metadata": {"run_id": "defer-fail-run"},
    }
    error = RuntimeError("sandbox_gateway_model_failed")
    error.gateway_logical_operation_id = "op-marker-failed"  # type: ignore[attr-defined]
    error.failure_usage = {"input_tokens": 8, "output_tokens": 0}  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="sandbox_gateway_model_failed"):
        await invoke_model_with_retry_observability(
            _GatewayLikeModel(error=error),
            [HumanMessage(content="q")],
            config,
            span_name="gateway.researcher.model",
            agent_role="researcher",
            model_name="openai:gpt-test",
            max_attempts=1,
        )

    assert SQLiteTraceStore(str(trace_path))._usage_rows("defer-fail-run") == []  # noqa: SLF001


def _gateway_config(trace_path) -> dict[str, Any]:
    return {
        "configurable": {
            "trace_store_path": str(trace_path),
            "event_log_enabled": False,
            "sandbox_root_signing_key": _ROOT_KEY,
        },
        "metadata": {
            "run_id": "forward-run",
            "task_id": "forward-task",
            "run_fence_token": 3,
        },
    }


@pytest.mark.asyncio
async def test_gateway_tool_usage_forwards_to_internal_api(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_GATEWAY_PHYSICAL_PROCESS", "true")
    monkeypatch.setenv("SANDBOX_API_INTERNAL_URL", "http://api:2024")
    posts: list[tuple[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url, root_key):
            self.base_url = base_url

        def signed(self, model_type, **values):
            return model_type(
                **values,
                service_timestamp=time.time(),
                service_nonce=secrets.token_urlsafe(24),
                service_signature="fake",
            )

        async def post(self, path, request):
            posts.append((path, request))
            return {"status": "recorded", "revision": 1}

    monkeypatch.setattr(internal_api_module, "SandboxInternalClient", FakeClient)
    trace_path = tmp_path / "forward.sqlite3"
    response = AIMessage(
        content="summary",
        usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
    )

    await invoke_model_with_retry_observability(
        _GatewayLikeModel(response=response),
        [HumanMessage(content="q")],
        _gateway_config(trace_path),
        span_name="tool.tavily.summarize_webpage",
        agent_role="researcher",
        model_name="openai:gpt-test",
        stage="researching",
        max_attempts=1,
    )

    # Task-activity forwarding shares the same patched client; isolate the
    # usage-report posts before asserting on them.
    usage_posts = [
        (path, request)
        for path, request in posts
        if path == "/internal/sandbox/usage/report"
    ]
    assert len(usage_posts) == 1
    path, request = usage_posts[0]
    assert request.run_id == "forward-run"
    assert request.task_id == "forward-task"
    assert request.fence_token == 3
    assert request.stage == "researching"
    assert request.input_tokens == 120
    assert request.output_tokens == 30
    assert request.usage_source == "provider_reported"
    assert request.response_status == "success"
    assert request.event_key.startswith("gateway-tool:")
    assert SQLiteTraceStore(str(trace_path))._usage_rows("forward-run") == []  # noqa: SLF001


@pytest.mark.asyncio
async def test_gateway_logical_rpc_model_calls_do_not_forward(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_GATEWAY_PHYSICAL_PROCESS", "true")
    monkeypatch.setenv("SANDBOX_API_INTERNAL_URL", "http://api:2024")
    posts: list[tuple[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url, root_key):
            del base_url, root_key

        def signed(self, model_type, **values):
            return model_type(
                **values,
                service_timestamp=time.time(),
                service_nonce=secrets.token_urlsafe(24),
                service_signature="fake",
            )

        async def post(self, path, request):
            posts.append((path, request))
            return {"status": "recorded", "revision": 1}

    monkeypatch.setattr(internal_api_module, "SandboxInternalClient", FakeClient)
    response = AIMessage(
        content="rpc",
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )

    await invoke_model_with_retry_observability(
        _GatewayLikeModel(response=response),
        [HumanMessage(content="q")],
        _gateway_config(tmp_path / "rpc.sqlite3"),
        span_name="gateway.researcher.model",
        agent_role="researcher",
        model_name="openai:gpt-test",
        stage="researching",
        attributes={"gateway": True, "logical_operation_id": "op-rpc"},
        max_attempts=1,
    )

    assert [
        path for path, _request in posts if path == "/internal/sandbox/usage/report"
    ] == []


@pytest.mark.asyncio
async def test_gateway_tool_failure_forwards_rejected_status(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_GATEWAY_PHYSICAL_PROCESS", "true")
    monkeypatch.setenv("SANDBOX_API_INTERNAL_URL", "http://api:2024")
    posts: list[tuple[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url, root_key):
            del base_url, root_key

        def signed(self, model_type, **values):
            return model_type(
                **values,
                service_timestamp=time.time(),
                service_nonce=secrets.token_urlsafe(24),
                service_signature="fake",
            )

        async def post(self, path, request):
            posts.append((path, request))
            return {"status": "recorded", "revision": 1}

    monkeypatch.setattr(internal_api_module, "SandboxInternalClient", FakeClient)

    with pytest.raises(RuntimeError, match="provider boom"):
        await invoke_model_with_retry_observability(
            _GatewayLikeModel(error=RuntimeError("provider boom")),
            [HumanMessage(content="q")],
            _gateway_config(tmp_path / "fail.sqlite3"),
            span_name="tool.tavily.summarize_webpage",
            agent_role="researcher",
            model_name="openai:gpt-test",
            stage="researching",
            max_attempts=1,
        )

    usage_posts = [
        request
        for path, request in posts
        if path == "/internal/sandbox/usage/report"
    ]
    assert len(usage_posts) == 1
    request = usage_posts[0]
    assert request.response_status == "rejected"
    assert request.usage_source == "missing"


def test_usage_report_endpoint_records_and_dedupes(tmp_path):
    context = _context(tmp_path)
    app = FastAPI()
    app.include_router(build_internal_sandbox_router(lambda _run_id: context))

    def _usage_report(event_key: str):
        return _signed(
            UsageReportRequest,
            run_id="backfill-run",
            task_id="task-1",
            fence_token=1,
            stage="researching",
            agent_role="researcher",
            provider="openai",
            model="gpt-test",
            operation="tool.tavily.summarize_webpage",
            event_key=event_key,
            attempt_index=1,
            input_tokens=44,
            output_tokens=6,
            total_tokens=50,
            usage_source="provider_reported",
            response_status="success",
        )

    with TestClient(app) as client:
        first = client.post(
            "/internal/sandbox/usage/report",
            json=_usage_report("gateway-tool:span-1:1:success").model_dump(mode="json"),
        )
        second = client.post(
            "/internal/sandbox/usage/report",
            json=_usage_report("gateway-tool:span-1:1:success").model_dump(mode="json"),
        )

    assert first.status_code == 200
    assert first.json()["status"] == "recorded"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    rows = _store(tmp_path)._usage_rows("backfill-run")  # noqa: SLF001
    assert len(rows) == 1
    assert rows[0]["event_key"] == "gateway-tool:span-1:1:success"
    assert rows[0]["input_tokens"] == 44
    assert rows[0]["operation"] == "tool.tavily.summarize_webpage"


def _write_journal_record(runs_dir: Path, run_id: str, record: dict[str, Any]) -> None:
    root = runs_dir / run_id / "sandbox" / "model_operations"
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(record["logical_operation_id"].encode()).hexdigest()
    (root / f"{key}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )


def test_reconcile_backfills_historical_journal(tmp_path):
    internal_api_module._RECONCILED_JOURNAL_SIGNATURES.clear()  # noqa: SLF001
    runs_dir = tmp_path / "runs"
    context = _context(tmp_path)
    completed = _record(operation_id="op-hist-1", status="completed")
    failed = _record(operation_id="op-hist-2", status="failed", usage={"input_tokens": 4, "output_tokens": 1})
    reserved = _record(operation_id="op-hist-3", status="reserved")
    for record in (completed, failed, reserved):
        _write_journal_record(runs_dir, "backfill-run", record)
    # op-hist-1 was already backfilled by its transition replay.
    backfill_gateway_usage_event(context, completed)

    written = reconcile_run_gateway_usage(
        "backfill-run",
        runs_dir=str(runs_dir),
        config=context.config,
    )

    assert written == 1
    rows = {
        row["event_key"]: row
        for row in _store(tmp_path)._usage_rows("backfill-run")  # noqa: SLF001
    }
    assert set(rows) == {"op-hist-1", "op-hist-2"}
    assert rows["op-hist-2"]["response_status"] == "rejected"
    assert rows["op-hist-2"]["input_tokens"] == 4


def test_reconcile_signature_cache_and_new_files(tmp_path):
    internal_api_module._RECONCILED_JOURNAL_SIGNATURES.clear()  # noqa: SLF001
    runs_dir = tmp_path / "runs"
    context = _context(tmp_path)
    _write_journal_record(runs_dir, "cache-run", _record(run_id="cache-run", operation_id="op-cache-1"))

    first = reconcile_run_gateway_usage("cache-run", runs_dir=str(runs_dir), config=context.config)
    cached = reconcile_run_gateway_usage("cache-run", runs_dir=str(runs_dir), config=context.config)
    _write_journal_record(
        runs_dir,
        "cache-run",
        _record(run_id="cache-run", operation_id="op-cache-2", status="failed"),
    )
    after_new_file = reconcile_run_gateway_usage("cache-run", runs_dir=str(runs_dir), config=context.config)

    assert first == 1
    assert cached == 0
    assert after_new_file == 1
    keys = {
        row["event_key"]
        for row in _store(tmp_path)._usage_rows("cache-run")  # noqa: SLF001
    }
    assert keys == {"op-cache-1", "op-cache-2"}


def test_reconcile_missing_journal_dir_returns_zero(tmp_path):
    assert (
        reconcile_run_gateway_usage(
            "no-such-run", runs_dir=str(tmp_path / "runs"), config=None
        )
        == 0
    )


def test_load_run_usage_response_reconciles_history(tmp_path):
    internal_api_module._RECONCILED_JOURNAL_SIGNATURES.clear()  # noqa: SLF001
    from open_deep_research import server as server_module

    runs_dir = tmp_path / "runs"
    trace_path = tmp_path / "traces.sqlite3"
    _write_journal_record(
        runs_dir,
        "server-run",
        _record(run_id="server-run", operation_id="op-server-1", usage={"input_tokens": 700, "output_tokens": 90}),
    )
    configurable = Configuration(
        token_usage_accounting_enabled=True,
        observability_enabled=False,
        sqlite_observability_enabled=False,
        trace_store_path=str(trace_path),
        runs_dir=str(runs_dir),
        event_log_enabled=False,
    )

    response = server_module._load_run_usage_response(  # noqa: SLF001
        "server-run",
        status="completed",
        configurable=configurable,
    )

    assert response["totals"]["reported"]["input_tokens"] == 700
    assert response["totals"]["reported"]["output_tokens"] == 90
    assert response["totals"]["calls"]["attempts"] == 1


@pytest.mark.asyncio
async def test_forward_keys_unique_without_span_id(tmp_path, monkeypatch):
    """Gateway noop spans must not collapse forwarded rows onto shared keys."""
    monkeypatch.setenv("SANDBOX_GATEWAY_PHYSICAL_PROCESS", "true")
    monkeypatch.setenv("SANDBOX_API_INTERNAL_URL", "http://api:2024")
    posts: list[tuple[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url, root_key):
            del base_url, root_key

        def signed(self, model_type, **values):
            return model_type(
                **values,
                service_timestamp=time.time(),
                service_nonce=secrets.token_urlsafe(24),
                service_signature="fake",
            )

        async def post(self, path, request):
            posts.append((path, request))
            return {"status": "recorded", "revision": 1}

    monkeypatch.setattr(internal_api_module, "SandboxInternalClient", FakeClient)
    # Observability disabled: the recorder is a noop context whose span_id is
    # None — the exact condition the Gateway process runs under.
    config = {
        "configurable": {
            "token_usage_accounting_enabled": False,
            "observability_enabled": False,
            "sqlite_observability_enabled": False,
            "sandbox_root_signing_key": _ROOT_KEY,
        },
        "metadata": {
            "run_id": "forward-noop-run",
            "task_id": "forward-noop-task",
            "run_fence_token": 2,
        },
    }

    for _ in range(2):
        with pytest.raises(RuntimeError, match="provider boom"):
            await invoke_model_with_retry_observability(
                _GatewayLikeModel(error=RuntimeError("provider boom")),
                [HumanMessage(content="q")],
                config,
                span_name="web.rerank",
                agent_role="researcher",
                model_name="openai:gpt-test",
                stage="researching",
                max_attempts=1,
            )

    usage_posts = [
        request
        for path, request in posts
        if path == "/internal/sandbox/usage/report"
    ]
    assert len(usage_posts) == 2
    keys = {request.event_key for request in usage_posts}
    assert len(keys) == 2
    assert all("None" not in key for key in keys)
