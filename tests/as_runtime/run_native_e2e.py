"""Run the explicit production E2E stack and stop only containers it starts."""

import os
import shutil
import subprocess
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    for name in (
        "AS_TEAM_WORKER_ENV_FILE",
        "AS_ROCKETMQ_ENDPOINT",
        "E2E_SOURCE_SELECTIONS",
    ):
        if not env.get(name):
            raise SystemExit(f"请配置 {name}；部署说明见 deploy/native-e2e.md")
    env.setdefault("COMPOSE_PROJECT_NAME", "insightforge-native-e2e")
    env["E2E_NATIVE_FULL"] = "true"
    env["E2E_MANAGED_STACK"] = "true"
    env.setdefault("PLAYWRIGHT_BASE_URL", "http://127.0.0.1:8080")
    compose = [
        "docker",
        "compose",
        "--project-directory",
        str(root),
        "-f",
        "docker-compose.yaml",
        "-f",
        "deploy/compose.native-e2e.yaml",
        "--profile",
        "sandbox",
    ]

    def run(args, **kwargs):
        return subprocess.run(args, cwd=root, env=env, check=True, **kwargs)

    existing = run(
        [*compose, "ps", "-q", "--status", "running"], capture_output=True, text=True
    ).stdout.strip()
    if existing:
        raise SystemExit(
            "E2E 专用项目已有运行中容器；先停止本次验收项目或改用独立 COMPOSE_PROJECT_NAME。"
        )
    team_filter = [
        "docker",
        "ps",
        "-aq",
        "--filter",
        "label=insightforge.role=native-team",
        "--filter",
        f"label=insightforge.project={env['COMPOSE_PROJECT_NAME']}",
    ]
    previous_teams = set(
        run(team_filter, capture_output=True, text=True).stdout.split()
    )
    pnpm = shutil.which("pnpm")
    if not pnpm:
        raise SystemExit("pnpm 不可用")
    try:
        run(
            [
                *compose,
                "build",
                "api",
                "sandbox-controller",
                "sandbox-gateway",
                "frontend",
                "publisher-worker",
            ]
        )
        run(
            [
                *compose,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "180",
                "api",
                "sandbox-controller",
                "sandbox-gateway",
                "frontend",
                "proxy",
                "publisher-worker",
            ]
        )
        run(
            [
                pnpm,
                "--dir",
                "frontend",
                "exec",
                "playwright",
                "test",
                "native-research.spec.ts",
                "--project=desktop",
                "--workers=1",
            ]
        )
    finally:
        # 专用项目启动前没有运行中容器；停止它的容器，保留数据库和报告卷供验收审计。
        subprocess.run(
            [*compose, "stop", "--timeout", "30"], cwd=root, env=env, check=False
        )
        # Controller may stop before API drain finishes; reclaim only newly created teams.
        teams = subprocess.run(
            team_filter, cwd=root, env=env, capture_output=True, text=True, check=False
        )
        for container_id in set(teams.stdout.split()) - previous_teams:
            subprocess.run(
                ["docker", "rm", "--force", container_id],
                cwd=root,
                env=env,
                check=False,
            )


if __name__ == "__main__":
    main()
