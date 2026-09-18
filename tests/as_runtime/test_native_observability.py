"""Native receipt projection, repeatable scraping and content-free SDK hooks."""

import json

import pytest
from agentscope.agent import Agent
from agentscope.message import TextBlock, UserMsg
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_recovery import create, store  # noqa: F401
from test_research_migration import ScriptedModel

from open_deep_research.agentscope_runtime import telemetry
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.usage_projection import (
    project_usage,
    timeline,
)
from open_deep_research.observability.tracing import SQLiteTraceStore

pytestmark = pytest.mark.asyncio


def empty_response():
    return {"totals": SQLiteTraceStore._empty_accounting_totals(), "operations": {}}


async def test_receipts_group_by_role_task_time_without_counting_gateway_twice(store):
    state, lease = await create(store)
    for key, role, task in (
        ("a", "researcher", "t1"),
        ("b", "quality_evaluation", "t2"),
    ):
        await store.begin_operation(
            lease,
            key,
            "gateway:model",
            key,
            reserve={"model_calls": 1},
            observation={
                "task_id": task,
                "stage": "researching",
                "agent_role": role,
                "model": "m",
                "prompt": "must not persist",
            },
        )
        receipt = {
            "status": "completed",
            "outcome": {"usage": {"input_tokens": 10, "output_tokens": 2}},
        }
        await store.commit_operation(
            lease,
            key,
            receipt,
            actual={"model_calls": 1, "input_tokens": 10, "output_tokens": 2},
        )
        await store.commit_operation(lease, key, receipt)
    for key, kind, reservation, error in (
        ("remote", "gateway:tool", {"tool_calls": 1}, None),
        ("local-copy", "tool", {}, None),
        ("failed", "tool", {"tool_calls": 1}, {"error_type": "timeout"}),
    ):
        await store.begin_operation(
            lease,
            key,
            kind,
            key,
            reserve=reservation,
            observation={
                "task_id": "t1",
                "stage": "researching",
                "tool_name": "fetch_url",
            },
        )
        receipt = (
            {"outcome": {"error": error}}
            if kind == "gateway:tool"
            else {"error": error}
        )
        await store.commit_operation(lease, key, receipt)
    await store.begin_operation(
        lease,
        "pending",
        "gateway:model",
        "p",
        reserve={"model_calls": 1},
        observation={"task_id": "t2"},
    )
    usage = await project_usage(store, state.run_id, "owner", empty_response())
    assert usage["totals"]["reported"]["total_tokens"] == 24
    assert usage["totals"]["calls"]["attempts"] == 3
    assert usage["totals"]["calls"]["unknown_failed_attempts"] == 0
    assert usage["operations"]["tool_success_rate"] == 0.5
    assert usage["operations"]["tool_call_count"] == 2
    assert usage["task_operations"]["t1"]["tool_call_count"] == 2
    assert usage["timeline"][-1]["reported_cumulative"] == 24
    assert {item["key"] for item in usage["breakdowns"]["by_agent_role"]} == {
        "researcher",
        "quality_evaluation",
    }
    assert "must not persist" not in json.dumps(
        await store.events(state.run_id, "owner")
    )
    with pytest.raises(KeyError):
        await project_usage(store, state.run_id, "other", empty_response())
    first = await telemetry.prometheus_snapshot(store)
    assert first == await telemetry.prometheus_snapshot(store)
    assert b'insightforge_native_tools{status="success"} 1.0' in first
    assert b'insightforge_native_tokens{direction="input_tokens"} 20.0' in first
    assert state.run_id.encode() not in first


async def test_legacy_receipts_keep_unknown_dimensions_and_no_fake_tool_success(store):
    state, lease = await create(store)
    await store.begin_operation(lease, "old", "gateway:model", "old")
    await store.commit_operation(
        lease,
        "old",
        {
            "status": "completed",
            "outcome": {"usage": {"input_tokens": 1, "output_tokens": 1}},
        },
    )
    usage = await project_usage(store, state.run_id, "owner", empty_response())
    assert usage["breakdowns"]["by_task"][0]["key"] == "unknown"
    assert usage["operations"]["tool_success_rate"] is None
    assert (
        len(usage["timeline"]) == 1
    )  # Old completion timestamps were already durable.


async def test_timeline_is_bounded_and_conserves_totals():
    records = [
        {
            "finished_at": i * 7,
            "reported": {"total_tokens": 2},
            "estimated": {"total_tokens": 3},
            "retry_count": 0,
        }
        for i in range(1000)
    ]
    buckets = timeline(records)
    assert len(buckets) <= 120
    assert buckets[-1]["reported_cumulative"] == 2000
    assert buckets[-1]["estimated_cumulative"] == 3000


async def test_native_reply_and_operations_export_no_content_or_exception_text(
    store, monkeypatch
):
    sink = TracerProvider()
    exporter = InMemorySpanExporter()
    sink.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "provider", lambda: sink)
    state, lease = await create(store)
    recovery = RecoverySession(store, lease, state)
    middleware = telemetry.NativeTelemetryMiddleware(recovery, "researcher")

    async def reply(**kwargs):
        with telemetry.operation_span("model", recovery, "researcher"):
            yield "private-output"

    assert [
        item async for item in middleware.on_reply(None, {"prompt": "secret"}, reply)
    ] == ["private-output"]
    try:
        with telemetry.operation_span("tool", recovery):
            raise ValueError("secret-key-in-error")
    except ValueError:
        pass
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} == {"agent.reply", "native.model", "native.tool"}
    model = next(s for s in spans if s.name == "native.model")
    agent = next(s for s in spans if s.name == "agent.reply")
    assert model.parent.span_id == agent.context.span_id
    encoded = str([s.to_json() for s in spans])
    assert "private-output" not in encoded and "secret" not in encoded
    assert state.run_id in encoded
    sink.shutdown()


async def test_real_agentscope_reply_runs_native_telemetry_hook(store, monkeypatch):
    sink = TracerProvider()
    exporter = InMemorySpanExporter()
    sink.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "provider", lambda: sink)
    state, lease = await create(store)
    recovery = RecoverySession(store, lease, state)
    agent = Agent(
        name="researcher",
        system_prompt="private system",
        model=ScriptedModel([[TextBlock(text="private answer")]]),
        middlewares=[telemetry.NativeTelemetryMiddleware(recovery, "researcher")],
    )
    await agent.reply(UserMsg("user", "private question"))
    spans = exporter.get_finished_spans()
    assert len(spans) == 1 and spans[0].name == "agent.reply"
    assert "private" not in spans[0].to_json()
    sink.shutdown()


async def test_otlp_http_exports_to_collector_and_langfuse_with_no_content(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from open_deep_research.configuration import Configuration

    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = ExportTraceServiceRequest.FromString(body)
            received.append((self.path, self.headers.get("Authorization"), request))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    cfg = Configuration(
        observability_enabled=True,
        otel_enabled=True,
        otel_exporter_otlp_endpoint=base,
        langfuse_enabled=True,
        langfuse_base_url=base,
        langfuse_public_key="fixture-public",
        langfuse_secret_key="fixture-secret",
    )
    monkeypatch.setattr(telemetry.Configuration, "from_runnable_config", lambda _: cfg)
    telemetry.provider.cache_clear()
    sink = None
    try:
        sink = telemetry.provider()
        span = telemetry.start_span(
            "agent.reply", run_id="run", task_id="task", role="researcher"
        )
        span.end()
        assert sink.force_flush(timeout_millis=5000)
        assert {row[0] for row in received} == {
            "/v1/traces",
            "/api/public/otel/v1/traces",
        }
        langfuse = next(row for row in received if "/api/public" in row[0])
        assert langfuse[1].startswith("Basic ")
        assert all("fixture-secret" not in str(row[2]) for row in received)
        assert all(row[2].resource_spans for row in received)
    finally:
        if sink:
            sink.shutdown()
        telemetry.provider.cache_clear()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
