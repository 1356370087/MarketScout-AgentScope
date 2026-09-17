"""Read-only migration architecture/deployment checks; never authorize a cutover."""

from __future__ import annotations

import argparse
import ast
import json
import re
import tomllib
from pathlib import Path

APPLICATION_IMAGES = (
    "Dockerfile",
    "Dockerfile.publisher-worker",
    "Dockerfile.document-worker",
    "Dockerfile.sandbox-controller",
    "Dockerfile.sandbox-gateway",
    "Dockerfile.sandbox-worker",
)
LEGACY_ENGINES = {
    "open_deep_research.agents.query_engine",
    "open_deep_research.agents.deep_researcher",
}
# 原生侧禁止触碰的旧写入/事件/锁/审批界面（T041/T043/T044/T046 退出边界）。
# 旧引擎自身继续使用这些界面服务其历史运行；本规则只约束原生目录。
LEGACY_PATH_DOTTED = {
    "open_deep_research.agents.query_checkpoint",
    "open_deep_research.tasks.lease",
    "open_deep_research.run_control",
    "open_deep_research.events.public.RunEventPublisher",
    "open_deep_research.events.public.event_store_from_config",
    "open_deep_research.events.public.event_publisher_from_config",
    "open_deep_research.run_context.save_query_state",
    "open_deep_research.run_context.save_human_decision",
    "open_deep_research.run_context.load_human_decision",
}
# 仅原生运行时目录额外禁用：api/ 兼容层（T074）允许 RunEventStore 历史只读。
LEGACY_PATH_RUNTIME_ONLY = {
    "open_deep_research.events.public.RunEventStore",
}


def import_findings(
    path: Path, root: Path, *, native_scope: str | None = None
) -> list[dict]:
    """Inventory static and literal dynamic imports, including lazy imports.

    Lazy compatibility imports remain retirement debt even when not executed.
    This is a source guard, not a proof of arbitrary dynamic code safety.
    ``native_scope`` ("runtime" 或 "api") additionally rejects the legacy
    write/event/lock/approval surfaces that scope must never touch.
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    package = ".".join(path.relative_to(root / "src").parts[:-1])
    forbidden = set(LEGACY_PATH_DOTTED)
    if native_scope == "runtime":
        forbidden |= LEGACY_PATH_RUNTIME_ONLY
    findings = []
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                parents = package.split(".")
                module = ".".join(
                    parents[: len(parents) - node.level + 1]
                    + ([module] if module else [])
                )
            modules = [module, *(module + "." + alias.name for alias in node.names)]
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name in {"__import__", "import_module"} and isinstance(
                node.args[0].value, str
            ):
                modules = [node.args[0].value]
        for module in modules:
            kind = (
                "langchain_import"
                if module.split(".")[0] == "langchain"
                or module.split(".")[0].startswith("langchain_")
                else "legacy_engine_import"
                if module in LEGACY_ENGINES
                else "legacy_path_import"
                if native_scope and module in forbidden
                else None
            )
            if kind and not any(
                item["line"] == node.lineno and item["kind"] == kind
                for item in findings
            ):
                findings.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "line": node.lineno,
                        "kind": kind,
                        "module": module,
                    }
                )
    return findings


def deployment_findings(root: Path) -> list[dict]:
    """Check application interpreter selection and declared optional dependencies."""
    target = (root / ".python-version").read_text().strip()
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    extras = project.get("optional-dependencies", {})
    findings = []
    for name in APPLICATION_IMAGES:
        text = (root / name).read_text(encoding="utf-8")
        bases = re.findall(
            r"^FROM\s+python:([^\s]+)", text, re.MULTILINE | re.IGNORECASE
        )
        if not bases or any(
            not base.startswith(target + "-") and base != target for base in bases
        ):
            findings.append(
                {
                    "path": name,
                    "kind": "python_version_mismatch",
                    "expected": target,
                    "actual": bases,
                }
            )
        if "UV_PYTHON_DOWNLOADS=never" not in text:
            findings.append({"path": name, "kind": "implicit_interpreter_download"})
        for extra in re.findall(r"--extra\s+([\w-]+)", text):
            if extra not in extras:
                findings.append(
                    {"path": name, "kind": "undefined_dependency_extra", "extra": extra}
                )
    return findings


def audit(root: Path, check: str = "all") -> dict:
    """Return actionable locations without reading configuration secrets or run data."""
    root = root.resolve()
    findings = []
    if check in {"all", "native", "retirement"}:
        paths = (root / "src").rglob("*.py")
        for path in sorted(paths):
            relative = path.relative_to(root).as_posix()
            native_scope = None
            if relative.startswith("src/open_deep_research/agentscope_runtime/"):
                native_scope = "runtime"
            elif relative.startswith("src/open_deep_research/api/"):
                native_scope = "api"
            if check == "native" and not native_scope:
                continue
            findings.extend(import_findings(path, root, native_scope=native_scope))
    if check in {"all", "deployment"}:
        findings.extend(deployment_findings(root))
    return {
        "schema_version": 1,
        "check": check,
        "passed": not findings,
        "findings": findings,
        "scope": "static_only_not_cutover_acceptance",
    }


def main(argv=None) -> int:
    """Run with --check native during migration; retirement must pass before removal."""
    parser = argparse.ArgumentParser(
        description="只读检查迁移架构与镜像，不读取 .env 或执行切换"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--check", choices=("native", "retirement", "deployment", "all"), default="all"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = audit(args.root, args.check)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
