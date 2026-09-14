"""Exercise Worker activity export through Controller archive collection."""

import asyncio
import io
import json
import tarfile
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from open_deep_research.events.task_activity import (
    TaskActivityStore,
    activity_summary,
    publish_task_activity,
)
from open_deep_research.sandbox import worker
from open_deep_research.sandbox.controller import DockerControllerRuntime
from open_deep_research.sandbox.manager import DockerSandboxManager
from open_deep_research.sandbox.wire import SandboxTaskPayloadV1


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_litellm_worker_model_calls_publish_activity(monkeypatch, tmp_path, fails):
    from open_deep_research.models import invocation
    from open_deep_research.models.gateway import (
        ModelGatewayError,
        ModelResult,
        ModelRoute,
        ModelUsage,
    )

    async def complete(_request):
        if fails:
            raise ModelGatewayError("gateway_unavailable")
        return ModelResult(
            message=AIMessage(content="private response"), structured=None,
            usage=ModelUsage(input_tokens=5, output_tokens=2, total_tokens=7),
            response_cost_usd=None, request_id="request-1",
            route=ModelRoute(requested_model="if-researcher-v1", served_model="served-model"),
            finish_reason="stop", latency_ms=10,
        )

    monkeypatch.setattr(invocation, "get_model_gateway", lambda _: SimpleNamespace(complete=complete))
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    config = {"configurable": {"runs_dir": str(tmp_path)}, "metadata": {"run_id": "run-1", "task_id": "task-1"}}
    call = invocation.complete_model(
        [HumanMessage(content="private prompt")], config,
        role="researcher", stage="researching", model="if-researcher-v1", span_name="researcher.model",
        max_output_tokens=128,
    )
    if fails:
        with pytest.raises(ModelGatewayError):
            await call
    else:
        await call
    events = TaskActivityStore("run-1", "task-1", runs_dir=str(tmp_path)).read()
    assert [event.type for event in events] == ["model.started", "model.failed" if fails else "model.completed"]
    assert "private" not in str([event.public_dict() for event in events])
    if not fails:
        assert activity_summary(events)["model_call_count"] == 1
        assert events[-1].payload["input_tokens"] == 5


@pytest.mark.parametrize("fails", [False, True])
def test_worker_exports_activity_on_success_and_failure(monkeypatch, tmp_path, fails):
    from open_deep_research.agents import deep_researcher

    runtime_root = tmp_path / "worker-runs"
    output_root = tmp_path / "output"
    payload = SandboxTaskPayloadV1(
        run_id="run-1", task_id="task-1", research_topic="topic",
        researcher_state={"researcher_messages": []},
        runtime_config={"runs_dir": str(runtime_root)},
        profile_id="research", policy_digest="digest", fence_token=1,
    )
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(payload.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("SANDBOX_TASK_PAYLOAD_PATH", str(payload_path))
    monkeypatch.setenv("SANDBOX_RESULT_PATH", str(output_root / "result.json"))
    monkeypatch.setenv("RUNS_DIR", str(runtime_root))
    monkeypatch.setattr(worker, "_setup_logging", lambda: None)

    async def invoke(_state, config):
        await publish_task_activity(
            config, "quality.failed", kind="error", phase="quality_check",
            status="error", title="质量评估不可用", summary="",
            iteration=None, duration_ms=None,
            payload={"evaluation_type": "tool_result", "error_code": "timeout"},
            dedupe_key="quality:batch-1",
        )
        if fails:
            raise RuntimeError("test worker failure")
        return {"compressed_research": "done"}

    monkeypatch.setattr(deep_researcher, "researcher_runtime", SimpleNamespace(ainvoke=invoke))
    assert asyncio.run(worker._run()) == int(fails)
    exported = output_root / "task_activity.jsonl"
    assert exported.exists()
    original = TaskActivityStore("run-1", "task-1", runs_dir=str(runtime_root)).read()[0]
    assert json.loads(exported.read_text(encoding="utf-8"))["event_id"] == original.event_id


def test_collected_worker_activity_is_durable_deduplicated_and_scoped(tmp_path):
    worker_store = TaskActivityStore("run-1", "task-1", runs_dir=str(tmp_path / "worker"))
    for kind, phase, event_type in [
        ("model", "reasoning", "model.completed"),
        ("tool", "tool_execution", "tool.completed"),
        ("quality", "quality_check", "quality.completed"),
    ]:
        asyncio.run(worker_store.append(
            event_type, kind=kind, phase=phase, status="success", title="activity",
            summary="", iteration=1, duration_ms=10, payload={}, dedupe_key=event_type,
        ))
    worker_events = worker_store.read()
    resources = SimpleNamespace(
        output_bytes=1024 * 1024, log_bytes=1024 * 1024,
        artifact_bytes=1024 * 1024, max_files=100,
    )

    class Container:
        id = "worker-1"

        def get_archive(self, path):
            # Docker's archive API sees the underlying empty mountpoint, not tmpfs.
            return iter([b"\0" * 10240]), {}

        def exec_run(self, command, **kwargs):
            prefix = command[-1]
            return SimpleNamespace(exit_code=0, output=self.archive(prefix))

        def archive(self, path):
            prefix = path.rsplit("/", 1)[-1]
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as archive:
                if prefix == "output":
                    data = worker_store.path.read_bytes()
                    info = tarfile.TarInfo("output/task_activity.jsonl")
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            return buf.getvalue()

    controller = object.__new__(DockerControllerRuntime)
    controller._authorize_service = lambda *_args, **_kwargs: None
    controller._owned_worker = lambda _id: Container()
    controller._profile_for_container = lambda _: SimpleNamespace(resources=resources)
    controller._status = lambda _: SimpleNamespace(status="result_ready")
    archive = controller.collect_archive(SimpleNamespace(container_id="worker-1"))
    host_root = tmp_path / "api-runs"
    host = TaskActivityStore("run-1", "task-1", runs_dir=str(host_root))
    asyncio.run(host.append(
        "task.started", kind="lifecycle", phase="initializing", status="running",
        title="start", summary="", iteration=None, duration_ms=None,
        payload={}, dedupe_key="host:start",
    ))
    manager = DockerSandboxManager()
    for _ in range(2):
        manager._archive_controller_output(
            archive, "", runs_dir=str(host_root), run_id="run-1", task_id="task-1",
            profile=SimpleNamespace(resources=resources),
        )
    events = host.read()
    assert len(events) == 4
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert [(event.event_id, event.timestamp) for event in events[1:]] == [
        (event.event_id, event.timestamp) for event in worker_events
    ]
    assert activity_summary(events)["model_call_count"] == 1
    assert activity_summary(events)["tool_call_count"] == 1
    # An untrusted Worker cannot write into another task's durable stream.
    with pytest.raises(ValueError, match="task_activity_scope_mismatch"):
        host.import_events([worker_events[0].model_copy(update={"task_id": "other-task"})])
