"""Shared pytest isolation for project-local dotenv configuration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

# 离线测试显式注入配置，不能连接部署数据库或受真实 .env 影响。
# PostgreSQL/平台集成测试在各自 fixture 中覆盖此模式。
os.environ.setdefault("PLATFORM_CONFIGURATION_MODE", "explicit")
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

from open_deep_research.models.circuit import _reset_model_circuit_registry

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_AT_PYTEST_START = frozenset(os.environ)
_PROJECT_DOTENV_KEYS = frozenset(
    str(key)
    for key in dotenv_values(_PROJECT_ROOT / ".env")
    if key and key not in _ENV_AT_PYTEST_START
)


@pytest.fixture(autouse=True)
def isolate_project_dotenv_from_unit_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent server-import dotenv values from overriding per-test config."""
    for key in _PROJECT_DOTENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def isolate_model_circuit_registry() -> None:
    """Prevent process-local model health from leaking between tests."""
    _reset_model_circuit_registry()
    yield
    _reset_model_circuit_registry()
