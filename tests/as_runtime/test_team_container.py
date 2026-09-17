"""Real Docker kill/restart through the native external dispatch port."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from team_worker_fixture import workers
from test_team import env as team_env
from test_team_worker import assignment

from open_deep_research.agentscope_runtime.team_dispatch import DockerTeamLauncher

env = team_env
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("window", ["tool_committed", "handoff_committed"])
async def test_container_dispatch_kill_and_rejoin(env, tmp_path, pg_url, window):
    build, store, _, pool = env
    team = await build()
    host, _, marker = await workers(team, store, tmp_path)
    item, contract = assignment()
    await host.prepare(item, contract)
    async with pool.acquire() as db:
        schema = await db.fetchval("SELECT current_schema()")
    request = {
        "schema": schema,
        "url": pg_url.replace("127.0.0.1", "host.docker.internal"),
        "lease": asdict(team.lease),
        "leader_agent_id": team.leader_agent_id,
        "leader_session_id": team.leader_session_id,
        "root": "/evidence",
        "task_id": item.task_id,
        "window": window,
        "ready": "/evidence/ready.txt",
    }
    request_path = tmp_path / "worker.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    env_file = tmp_path / "worker.env"
    env_file.write_text("PYTHONPATH=/app/src:/app/tests/as_runtime\n", encoding="utf-8")
    launcher = DockerTeamLauncher(
        image="marketscout-m10-acceptance:local",
        command=[
            "python",
            "/app/tests/as_runtime/team_worker_fixture.py",
            "/evidence/worker.json",
        ],
        env_file=env_file,
        mounts=[
            f"type=bind,source={Path('src').resolve()},target=/app/src,readonly",
            f"type=bind,source={Path('tests').resolve()},target=/app/tests,readonly",
            f"type=bind,source={tmp_path.resolve()},target=/evidence",
        ],
    )
    host.external, host.launcher = True, launcher
    dispatch = asyncio.create_task(host.dispatch(item, contract))
    try:
        async with asyncio.timeout(40):
            while not (tmp_path / "ready.txt").exists():
                if dispatch.done():
                    dispatch.result()
                await asyncio.sleep(0.1)
        name = next(iter(launcher.containers))
        # Next container resumes the same durable task without the failpoint.
        request["window"] = ""
        request_path.write_text(json.dumps(request), encoding="utf-8")
        code, _ = await launcher.docker("kill", "--signal", "KILL", name)
        assert code == 0
        outcome = await asyncio.wait_for(dispatch, 40)
        assert outcome.assessment["handoff"]["accepted"]
        assert len(marker.read_text(encoding="utf-8").splitlines()) == 1
        assert launcher.containers[name] >= 1
        if window == "tool_committed":
            assert launcher.containers[name] == 2
    finally:
        dispatch.cancel()
        await asyncio.gather(dispatch, return_exceptions=True)
        await host.aclose()
