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
    result = audit(ROOT, "native")
    assert result["passed"], result["findings"]


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
for name in (
    "agentscope_runtime.app", "agentscope_runtime.research", "agentscope_runtime.report",
    "agentscope_runtime.knowledge", "agentscope_runtime.memory", "agentscope_runtime.team_worker",
    "api.contracts", "api.history", "api.streams",
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
