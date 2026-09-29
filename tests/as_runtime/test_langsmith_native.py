"""Optional LangSmith transport must not reopen the old research or Judge stack."""

import os
import subprocess
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from tests import run_evaluate


@pytest.mark.asyncio
async def test_langsmith_target_uses_native_lifecycle_and_keeps_failure(monkeypatch, tmp_path):
    messages = [{"role": "user", "content": "Question"}, {"role": "user", "content": "Constraint"}]

    async def research(actual, config, **kwargs):
        assert actual == messages
        assert config["configurable"]["enable_human_in_loop"] is False
        assert kwargs["runs_dir"] == tmp_path / ".runs/langsmith/research"
        return "run-a", {"result": {"status": "error", "error": "failed"}, "final_report": "stale"}

    monkeypatch.setattr(run_evaluate, "ROOT", tmp_path)
    monkeypatch.setattr(run_evaluate, "run_native_question", research)
    state = await run_evaluate.target({"messages": messages})
    assert state["run_id"] == "run-a"
    assert state["result"]["status"] == "error"


@pytest.mark.asyncio
async def test_langsmith_preserves_native_metric_states_and_reference_answers(monkeypatch, tmp_path):
    closed = []

    async def score(**kwargs):
        assert kwargs["reference_outputs"] == {"answer": "golden"}
        assert kwargs["outputs"]["run_id"] == "run-a"
        return [
            {"key": "correctness_score", "score": 0.5, "comment": "checked", "status": "scored"},
            {"key": "groundedness_score", "score": None, "comment": "unavailable", "status": "not_scored"},
        ]

    @asynccontextmanager
    async def session(directory):
        assert directory.parent == tmp_path / ".runs/langsmith/judges"
        try:
            yield SimpleNamespace(score=score)
        finally:
            closed.append(True)

    monkeypatch.setattr(run_evaluate, "ROOT", tmp_path)
    monkeypatch.setattr(run_evaluate, "native_judge_session", session)
    result = await run_evaluate.evaluate_native({}, {"run_id": "run-a"}, {"answer": "golden"})
    assert result["results"][0]["score"] == 0.5
    assert result["results"][1]["score"] is None
    assert result["results"][1]["metadata"]["metric_status"] == "not_scored"
    assert closed == [True]


def test_langsmith_entry_import_is_native_without_optional_sdk():
    code = """
import sys
import tests.run_evaluate
assert not any(name.startswith('langchain') for name in sys.modules)
assert 'open_deep_research.agents.query_engine' not in sys.modules
assert 'langsmith' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": os.pathsep.join(['src', '.'])},
                   check=True, capture_output=True)
