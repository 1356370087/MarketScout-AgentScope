"""M11 import guard and deployment-audit regression checks."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from open_deep_research.migration_check import (
    APPLICATION_IMAGES,
    audit,
    deployment_findings,
    import_findings,
    main,
)

ROOT = Path(__file__).resolve().parents[2]


def test_native_source_has_no_legacy_imports():
    result = audit(ROOT, "retirement")
    assert result["passed"], result["findings"]


def test_retired_query_engine_and_file_task_executors_are_absent():
    for name in (
        "agents/query_engine.py", "agents/query.py", "agents/deep_researcher.py",
        "tasks/executor.py", "tasks/teammate_pool.py", "tasks/team_bridge.py",
        "tools/supervisor/__init__.py",
    ):
        assert not (ROOT / "src/open_deep_research" / name).exists()
    assert not [item for item in audit(ROOT, "retirement")["findings"] if item["kind"] == "legacy_engine_import"]


def test_application_images_match_interpreter_and_dependency_groups():
    assert deployment_findings(ROOT) == []


@pytest.mark.parametrize(
    "source",
    [
        "import langchain_core.messages as messages",
        "from langchain.chat_models import init_chat_model",
        "def old():\n from langchain_core.messages import AIMessage",
        "from open_deep_research.agents import query_engine as engine",
        "from ..agents.query_engine import QueryEngine",
        '__import__("langchain_core.messages")',
        'importlib.import_module("open_deep_research.agents.deep_researcher")',
    ],
)
def test_guard_detects_alias_lazy_relative_and_dynamic_imports(tmp_path, source):
    path = tmp_path / "src/open_deep_research/agentscope_runtime/sample.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    found = import_findings(path, tmp_path)
    assert found and found[0]["line"] > 0


def test_guard_rejects_legacy_write_event_lock_and_approval_surfaces(tmp_path):
    """原生侧旧路径禁令：写入/事件生产/锁/审批界面在 runtime 与 api 均被识别。"""
    sources = {
        "runtime": [
            "from open_deep_research.agents.query_checkpoint import RunContextQueryCheckpointSink",
            "from open_deep_research.events.public import RunEventPublisher",
            "from open_deep_research.events.public import event_store_from_config",
            "from open_deep_research.tasks.lease import LeaderLeaseManager",
            "import open_deep_research.run_control",
            "from open_deep_research.run_context import save_query_state",
            "from open_deep_research.run_context import save_human_decision",
        ],
        "api": [
            "from open_deep_research.events.public import RunEventPublisher",
            "from open_deep_research.run_context import save_query_state",
            "from open_deep_research.api_host.run_start import RunStartRoutes",
            "from open_deep_research import api_host",
            "from open_deep_research import server",
        ],
    }
    for scope, samples in sources.items():
        for source in samples:
            directory = (
                "src/open_deep_research/agentscope_runtime"
                if scope == "runtime"
                else "src/open_deep_research/api"
            )
            path = tmp_path / directory / "sample.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
            findings = import_findings(path, tmp_path, native_scope=scope)
            assert findings and findings[0]["kind"] == "legacy_path_import", source


def test_guard_allows_history_read_only_event_store_in_api_layer(tmp_path):
    """api/ 兼容层（T074）允许 RunEventStore 历史只读；runtime 目录仍禁用。"""
    source = "from open_deep_research.events.public import RunEventStore"
    api_path = tmp_path / "src/open_deep_research/api/streams.py"
    api_path.parent.mkdir(parents=True)
    api_path.write_text(source, encoding="utf-8")
    assert import_findings(api_path, tmp_path, native_scope="api") == []
    runtime_path = tmp_path / "src/open_deep_research/agentscope_runtime/x.py"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text(source, encoding="utf-8")
    findings = import_findings(runtime_path, tmp_path, native_scope="runtime")
    assert findings and findings[0]["kind"] == "legacy_path_import"


def test_comments_and_native_imports_are_not_legacy(tmp_path):
    path = tmp_path / "src/open_deep_research/sample.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# from langchain import old\nfrom agentscope import Agent\n", encoding="utf-8"
    )
    assert import_findings(path, tmp_path) == []


def test_deployment_guard_detects_version_and_undefined_extra(tmp_path):
    (tmp_path / ".python-version").write_text("3.14")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="fixture"\n')
    for name in APPLICATION_IMAGES:
        (tmp_path / name).write_text(
            "FROM python:3.12-slim\nRUN uv sync --extra document-worker\n"
        )
    kinds = {item["kind"] for item in deployment_findings(tmp_path)}
    assert kinds == {
        "python_version_mismatch",
        "implicit_interpreter_download",
        "undefined_dependency_extra",
    }
    for name in APPLICATION_IMAGES:
        (tmp_path / name).write_text(
            "FROM python:3.14-slim\nENV UV_PYTHON_DOWNLOADS=never\nRUN uv sync\n"
        )
    assert deployment_findings(tmp_path) == []


def test_failed_gate_writes_evidence_and_returns_nonzero(tmp_path):
    path = tmp_path / "src/open_deep_research/agentscope_runtime/sample.py"
    path.parent.mkdir(parents=True)
    path.write_text("import langchain_core")
    output = tmp_path / "result.json"
    assert (
        main(["--root", str(tmp_path), "--check", "native", "--output", str(output)])
        == 1
    )
    assert json.loads(output.read_text())["passed"] is False


def test_native_entry_imports_do_not_load_langchain_or_legacy_engine():
    # Separate interpreter catches transitive imports even when another test loaded legacy modules.
    code = """
import importlib, sys
from pathlib import Path
for package in ("agentscope_runtime", "api"):
    for path in Path("src/open_deep_research", package).glob("*.py"):
        if path.stem != "__init__":
            importlib.import_module("open_deep_research." + package + "." + path.stem)
for name in (
    "agentscope_runtime.app", "agentscope_runtime.research", "agentscope_runtime.report",
    "agentscope_runtime.knowledge", "agentscope_runtime.memory", "agentscope_runtime.team_worker",
    "api.contracts", "api.history", "api.streams",
    "api.native_runs", "api.research_router", "server",
):
    importlib.import_module("open_deep_research." + name)
assert not any(name.startswith("langchain") for name in sys.modules)
assert "open_deep_research.agents.query_engine" not in sys.modules
assert "open_deep_research.agents.deep_researcher" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
