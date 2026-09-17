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


def import_findings(path: Path, root: Path) -> list[dict]:
    """Inventory static and literal dynamic imports, including lazy imports.

    Lazy compatibility imports remain retirement debt even when not executed.
    This is a source guard, not a proof of arbitrary dynamic code safety.
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    package = ".".join(path.relative_to(root / "src").parts[:-1])
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
            if check == "native" and not relative.startswith(
                (
                    "src/open_deep_research/agentscope_runtime/",
                    "src/open_deep_research/api/",
                )
            ):
                continue
            findings.extend(import_findings(path, root))
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
