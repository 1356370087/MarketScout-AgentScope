"""Static guards for the V7 sandbox trust boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from open_deep_research.sandbox.manager import DockerSandboxManager

ROOT = Path(__file__).parents[1] / "src" / "open_deep_research"
WORKER_BOUNDARY = (
    ROOT / "sandbox" / "worker.py",
    ROOT / "sandbox" / "gateway_model.py",
    ROOT / "sandbox" / "gateway_tool.py",
    ROOT / "sandbox" / "local_provider.py",
)
FORBIDDEN_WORKER_IMPORTS = (
    "openai",
    "anthropic",
    "google.generativeai",
    "tavily",
    "open_deep_research.models.resolution",
    "open_deep_research.models.fallback",
    "open_deep_research.models.circuit",
)


def _imports(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((item.name, node.lineno) for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.lineno))
    return found


def test_worker_boundary_does_not_import_provider_or_physical_model_stacks() -> None:
    violations = []
    for path in WORKER_BOUNDARY:
        for module, line in _imports(path):
            if module.startswith(FORBIDDEN_WORKER_IMPORTS):
                violations.append(f"{path.name}:{line}:{module}")
    assert not violations, violations


def test_only_controller_opens_the_docker_sdk() -> None:
    violations = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        for module, line in _imports(path):
            if (module == "docker" or module.startswith("docker.")) and relative != "sandbox/controller.py":
                violations.append(f"{relative}:{line}:{module}")
    assert not violations, violations


def test_manager_rejects_legacy_docker_client_injection() -> None:
    """A dynamically supplied SDK client must never revive the host path."""
    with pytest.raises(RuntimeError, match="sandbox_controller_required"):
        DockerSandboxManager(docker_client=object())

    manager = DockerSandboxManager()
    with pytest.raises(RuntimeError, match="sandbox_controller_required"):
        manager._get_client()


def test_public_nginx_denies_internal_sandbox_control_plane() -> None:
    nginx = (
        ROOT.parents[1] / "deploy" / "research-console.nginx.conf"
    ).read_text(encoding="utf-8")
    assert "location = /api/research/internal/sandbox" in nginx
    assert "location ^~ /api/research/internal/sandbox/" in nginx
    assert nginx.count("return 404;") >= 2


def test_compose_shares_preset_egress_allowlist_across_control_plane() -> None:
    """API, Controller, and Gateway must freeze the same policy bundle."""
    compose = yaml.safe_load(
        (ROOT.parents[1] / "docker-compose.yaml").read_text(encoding="utf-8")
    )
    expected = "${SANDBOX_EGRESS_ALLOW_DOMAINS:-}"
    for service_name in ("api", "sandbox-controller", "sandbox-gateway"):
        environment = compose["services"][service_name]["environment"]
        assert environment["SANDBOX_EGRESS_ALLOW_DOMAINS"] == expected


def test_compose_keeps_gateway_api_and_proxy_on_distinct_ports() -> None:
    """The API endpoint and the forward proxy must not bind the same port."""
    compose = yaml.safe_load(
        (ROOT.parents[1] / "docker-compose.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    gateway = services["sandbox-gateway"]
    environment = gateway["environment"]

    assert environment["SANDBOX_GATEWAY_PORT"] == "8081"
    assert environment["SANDBOX_GATEWAY_PROXY_PORT"] == "8080"
    assert services["api"]["environment"]["SANDBOX_GATEWAY_URL"] == (
        "http://sandbox-gateway:8081"
    )
    assert "8081/healthz" in " ".join(gateway["healthcheck"]["test"])


def test_gateway_rpc_construction_declares_stage_operation_and_zone() -> None:
    required_by_type = {
        "GatewayModelRequestV1": {"stage", "logical_operation_id"},
        "GatewayToolRequestV1": {
            "stage",
            "logical_operation_id",
            "execution_zone",
        },
    }
    violations = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            required = required_by_type.get(node.func.id)
            if required is None:
                continue
            supplied = {keyword.arg for keyword in node.keywords if keyword.arg}
            missing = sorted(required - supplied)
            if missing:
                relative = path.relative_to(ROOT).as_posix()
                violations.append(f"{relative}:{node.lineno}:{','.join(missing)}")
    assert not violations, violations


def test_run_orchestration_never_constructs_provider_models_directly() -> None:
    """Provider constructors live only in resolution or Gateway-owned tools."""
    allowed = {
        "models/resolution.py",
        "tools/tavily_search/definition.py",
        "tools/tavily_search/summarization.py",
        "tools/web_research/pipeline.py",
    }
    violations = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "init_chat_model"
                and relative not in allowed
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert not violations, violations
