"""T048 部署组合故障矩阵：真实容器内的原生 API 宿主强杀与恢复。

覆盖窗口：
- ``approval_pause``：审批持久暂停时 ``docker kill``（外部强杀）→ 新容器
  继续服务，审批后完成；
- ``model_committed`` / ``tool_committed``：操作提交后容器内 ``os._exit(73)``
  （不经 finally）→ 新容器 resume 回放，外部副作用恰一次。
- ``report_committed``：原生报告模型结果提交后立即退出，新容器重放正文，报告模型只调用一次；
- ``approval_committed`` / ``event_committed``：审批消费或完成状态与事件
  入库后立即退出，验证重启后决策不丢失、完成事件可重放。

组件边界：PG 为真实容器（conftest 会话级），API 宿主为主镜像真实容器
（只读 bind mount 当前源码，uvicorn 真实 HTTP）。网关/LiteLLM 组合故障与
真实提供商未知结果对账不在本矩阵（需生产 LiteLLM 部署），跨主机网络分区
本地不可复现；团队 Worker 容器窗口见 test_team_container.py。
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore

pytestmark = pytest.mark.asyncio

IMAGE = "marketscout-m10-acceptance:local"
ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker {args} failed: {proc.stderr}")
    return proc


class ApiHostContainer:
    """命名容器 + 同挂载重启；请求文件驱动崩溃窗口。"""

    def __init__(self, name: str, evidence: Path, port: int, pg_dsn_container: str, schema: str):
        self.name = name
        self.evidence = evidence
        self.port = port
        self.pg_dsn = pg_dsn_container
        self.schema = schema
        self.request_path = evidence / "host-request.json"

    def _write_request(self, window: str) -> None:
        self.request_path.write_text(
            json.dumps(
                {
                    "database_url": self.pg_dsn,
                    "schema": self.schema,
                    "port": self.port,
                    "window": window,
                    "evidence_dir": "/evidence",
                }
            ),
            encoding="utf-8",
        )

    def start(self, window: str) -> None:
        self._write_request(window)
        _docker(
            "run",
            "-d",
            "--name",
            self.name,
            "--label",
            "insightforge.role=native-matrix",
            "-p",
            f"127.0.0.1:{self.port}:{self.port}",
            "-v",
            f"{ROOT / 'src'}:/app/src:ro",
            "-v",
            f"{ROOT / 'tests'}:/app/tests:ro",
            "-v",
            f"{self.evidence}:/evidence",
            "-e",
            "PYTHONPATH=/app/src:/app/tests/as_runtime",
            "-e",
            "APP_ENV=development",
            "-e",
            "LOCAL_DEV_AUTH_BYPASS=true",
            "-e",
            "PYTHONUNBUFFERED=1",
            IMAGE,
            "python",
            "/app/tests/as_runtime/deployment_api_host.py",
            "/evidence/host-request.json",
        )

    def kill(self) -> None:
        _docker("kill", "--signal", "KILL", self.name)

    def exit_code(self) -> int | None:
        proc = _docker("inspect", "-f", "{{.State.ExitCode}} {{.State.Status}}", self.name)
        code, status = proc.stdout.strip().split()
        return None if status == "running" else int(code)

    def restart(self, window: str = "") -> None:
        _docker("rm", "-f", self.name, check=False)
        self.start(window)

    def remove(self) -> None:
        _docker("rm", "-f", self.name, check=False)

    async def wait_healthy(self, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{self.port}", trust_env=False
            ) as client:
            while time.monotonic() < deadline:
                exited = self.exit_code()
                if exited is not None:
                    logs = _docker("logs", "--tail", "60", self.name, check=False)
                    raise RuntimeError(
                        f"api host exited early ({exited}):\n{logs.stdout}\n{logs.stderr}"
                    )
                try:
                    response = await client.get("/healthz")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise TimeoutError("api host healthz timeout")

    async def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.port}", trust_env=False
        )


async def _wait_status(client, run_id, wanted, timeout=60.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = await client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in wanted:
            return last
        if last["status"] in {"failed", "cancelled"}:
            raise AssertionError(f"run entered unexpected terminal state: {last['status']}")
        await asyncio.sleep(0.5)
    events = await client.get(f"/runs/{run_id}/events")
    raise TimeoutError(
        f"run {run_id} stuck at {last and last['status']}; "
        f"pending={last and last.get('pending_human_action')}; "
        f"events={events.text[-600:]}"
    )


async def _budget_used(store: RecoveryStore, run_id: str, owner: str):
    budget = await store.budget(run_id, owner)
    return budget["used"]


async def _wait_lease_expired(store, run_id):
    # 崩溃没有 finally/release；只等待数据库租约自然过期，不更改 TTL/fence。
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        async with store.engine.connect() as conn:
            expires = await conn.scalar(select(store.runs.c.expires).where(
                store.runs.c.run_id == run_id
            ))
            if expires <= await store._now(conn):
                return
        await asyncio.sleep(0.5)
    raise TimeoutError("crashed host lease did not expire")


@pytest_asyncio.fixture
async def matrix(pg_url, tmp_path):
    schema = f"m6_matrix_{uuid.uuid4().hex[:8]}"
    engine = RecoveryStore(
        pg_url,
        engine_kwargs={"connect_args": {"server_settings": {"search_path": schema}}},
    )
    async with engine.engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    port = _free_port()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    container = ApiHostContainer(
        name=f"as-m6-matrix-{uuid.uuid4().hex[:8]}",
        evidence=evidence,
        port=port,
        pg_dsn_container=pg_url.replace("127.0.0.1", "host.docker.internal"),
        schema=schema,
    )
    try:
        yield container, engine, evidence
    finally:
        container.remove()
        await engine.aclose()


@pytest.mark.parametrize(
    "window",
    ["approval_pause", "model_committed", "tool_committed", "approval_committed", "event_committed", "report_committed", "review_committed"],
)
async def test_api_container_crash_windows_recover_exactly_once(matrix, window):
    """部署强杀窗口：副作用恰一次、决策持久化、事件与账本一致。"""
    container, engine, evidence = matrix
    container.start(window)
    await container.wait_healthy()
    async with await container.client() as client:
        body = {
            "messages": [{"role": "user", "content": "市场规模"}],
            "configurable": {"enable_human_in_loop": window in {"approval_pause", "approval_committed"}, "report_review_enabled": window == "review_committed", "report_review_fail_open": False},
        }
        response = await client.post("/runs", json=body)
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]

        if window == "approval_pause":
            paused = await _wait_status(client, run_id, {"awaiting_plan_approval"})
            action_id = paused["pending_human_action"]["action_id"]
            container.kill()
            await asyncio.sleep(0.5)
            container.restart()
            await container.wait_healthy()
            after = await _wait_status(client, run_id, {"awaiting_plan_approval"})
            assert after["pending_human_action"]["action_id"] == action_id
            await _wait_lease_expired(engine, run_id)
            decision = await client.post(
                f"/runs/{run_id}/human-actions/{action_id}",
                json={"action": "approve"},
            )
            assert decision.status_code == 200, decision.text
            outline = await _wait_status(client, run_id, {"awaiting_outline_approval"})
            decision = await client.post(
                f"/runs/{run_id}/human-actions/{outline['pending_human_action']['action_id']}",
                json={"action": "approve"},
            )
            assert decision.status_code == 200, decision.text
        else:
            if window == "approval_committed":
                paused = await _wait_status(client, run_id, {"awaiting_plan_approval"})
                action_id = paused["pending_human_action"]["action_id"]
                try:
                    decision = await client.post(
                        f"/runs/{run_id}/human-actions/{action_id}",
                        json={"action": "approve"},
                    )
                    assert decision.status_code == 200, decision.text
                except httpx.RemoteProtocolError:
                    pass  # The durable decision may precede the HTTP response.
            # 必须实际命中指定提交窗口，不能把正常退出算作故障注入成功。
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                code = container.exit_code()
                if code is not None:
                    assert code == 73, f"unexpected exit code {code}"
                    break
                await asyncio.sleep(0.5)
            else:
                raise TimeoutError(f"container did not self-exit for {window}")
            container.restart()
            await container.wait_healthy()
            await _wait_lease_expired(engine, run_id)
            if window != "event_committed":
                resume = await client.post(
                    f"/runs/{run_id}/resume", json={"configurable": {}}
                )
                assert resume.status_code == 202, resume.text
            if window == "approval_committed":
                outline = await _wait_status(client, run_id, {"awaiting_outline_approval"})
                snapshot, _ = await engine.load(run_id, "local-dev-user")
                assert action_id in snapshot.decisions
                decision = await client.post(
                    f"/runs/{run_id}/human-actions/{outline['pending_human_action']['action_id']}",
                    json={"action": "approve"},
                )
                assert decision.status_code == 200, decision.text
            if window in {"approval_committed", "event_committed", "report_committed", "review_committed"}:
                assert (evidence / "crash-window.txt").read_text().strip() == window

        completed = await _wait_status(client, run_id, {"completed"}, timeout=90)
        assert completed["output"]["markdown"]
        replay = await client.get(f"/runs/{run_id}/events")
        assert replay.text.count("event: run.completed") == 1

    effects = (evidence / "tool-effects.txt").read_text(encoding="utf-8").splitlines()
    assert effects == ["effect"], f"tool side effect repeated: {effects}"
    model_calls = (evidence / "model-calls.txt").read_text(encoding="utf-8")
    physical_calls = len(model_calls.splitlines())

    used = await _budget_used(engine, run_id, "local-dev-user")
    async with engine.engine.connect() as conn:
        tool_ops = (await conn.execute(select(engine.ops).where(
            engine.ops.c.run_id == run_id, engine.ops.c.kind == "tool",
            engine.ops.c.state == "committed",
        ))).mappings().all()
    # 委派 ConductResearch 本身也是被治理并计费的工具，外部搜索才是副作用。
    assert len(tool_ops) == 2
    assert used.get("tool_calls") == sum(row["actual"]["tool_calls"] for row in tool_ops)
    # 部署边界的账本一致性：物理模型调用数与 SQL 结算的 model_calls 相等。
    assert used.get("model_calls") == physical_calls, (used, physical_calls)

    if window == "report_committed":
        assert (evidence / "report-calls.txt").read_text(encoding="utf-8").splitlines() == ["final_report"]

    if window == "review_committed":
        assert (evidence / "review-calls.txt").read_text(encoding="utf-8").splitlines() == ["review"]
        assert completed["output"]["report_review"]["decision"] == "pass"
