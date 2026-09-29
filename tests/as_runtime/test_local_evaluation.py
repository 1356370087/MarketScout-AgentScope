"""Local entrypoint uses native SQL lifecycle without an old engine import."""

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.research_pipeline import (
    PendingDecision,
    ResearchPipeline,
)
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.evaluation import local_runtime
from tests.auth_helpers import research_principal


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,workers", [
    ("completed", []), ("failed", []), ("waiting", []), ("timeout", []), ("cancelled", []),
    ("failed", [("failed", "HTTPStatusError")]),
    ("failed", [("failed", "HTTPStatusError"), ("completed", None)]),
    ("failed", [("failed", "HTTPStatusError"), ("failed", "StructuredOutputError")]),
])
async def test_native_local_run_lifecycle(tmp_path, monkeypatch, outcome, workers):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    closed = []
    started = asyncio.Event()

    class Stages:
        async def execute(self, stage, state):
            if stage == "summarize_messages":
                started.set()
                if outcome in {"timeout", "cancelled"}:
                    await asyncio.Event().wait()
            if stage == "plan_approval" and outcome == "waiting":
                return PendingDecision(stage=stage, question="Confirm")
            if stage == "final_report_generation":
                state.final_report = "Full report"
                if outcome == "failed":
                    raise RuntimeError("fixture")

    @asynccontextmanager
    async def factory(state, config, recovery):
        try:
            yield ResearchPipeline(state, Stages(), recovery.save,
                                   config_fingerprint=state.config_fingerprint, recovery=recovery)
        finally:
            closed.append("pipeline")

    async def prepare(request, principal):
        return {"configurable": request.configurable}

    service = NativeRuns(store, factory, prepare, runs_dir=tmp_path)

    if workers:
        class Connection:
            async def fetch(self, query, run_id):
                return [{"task_id": str(i), "status": status, "error": error}
                        for i, (status, error) in enumerate(workers)]

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield Connection()

        factory.runtime = SimpleNamespace(_team_host=SimpleNamespace(pool=Pool()))

    async def close():
        await service.aclose()
        closed.append("service")

    service.native_aclose = close

    async def build(**kwargs):
        return service

    async def principal():
        return research_principal("local-evaluator")

    monkeypatch.setattr(local_runtime, "build_native_research_service", build)
    monkeypatch.setattr(local_runtime, "evaluation_principal", principal)
    try:
        task = asyncio.create_task(local_runtime.run_native_question(
            [{"role": "user", "content": "Research"}], {"configurable": {}},
            runs_dir=tmp_path, timeout=0.05 if outcome == "timeout" else 10,
        ))
        if outcome == "cancelled":
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            run_id, output = await task
            snapshot, _ = await store.load(run_id, "local-evaluator")
            expected = "cancelled" if outcome == "timeout" else outcome
            assert snapshot.status == output["runtime_status"] == expected
            assert output["engine"] == "agentscope"
            assert output["result"]["status"] == ("success" if outcome == "completed" else "error")
            if outcome == "waiting":
                assert snapshot.pending is not None
            if outcome == "timeout":
                assert output["result"]["error"] == "evaluation_research_timeout"
            assert bool(output.get("evaluation_error")) == (workers == [("failed", "HTTPStatusError")])
            if workers:
                failures = output["evaluation_snapshot"]["outcome"]["worker_failures"]
                assert len(failures) == sum(bool(error) for _, error in workers)
        assert closed == ["pipeline", "service"]
        assert not service.tasks
    finally:
        await store.aclose()


def test_local_entrypoint_never_imports_old_engine_or_langchain():
    env = {**os.environ, "PYTHONPATH": "src;."}
    code = """
import sys
import tests.run_local_evaluate
assert not any(name.startswith('langchain') for name in sys.modules)
assert 'open_deep_research.agents.query_engine' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True)
