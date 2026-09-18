"""Formal trusted Worker entrypoint against real PostgreSQL task state."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from team_worker_fixture import workers
from test_team import env as team_env
from test_team_worker import assignment

from open_deep_research.agentscope_runtime import team_executor
from open_deep_research.agentscope_runtime.production import RunResources
from open_deep_research.agentscope_runtime.run_config import RunConfig
from tests.auth_helpers import research_principal

env = team_env
pytestmark = pytest.mark.asyncio


async def test_formal_executor_rebuilds_persisted_task_without_releasing_leader(
    env, tmp_path, monkeypatch
):
    build, store, _, _ = env
    from test_research_migration import cfg
    run = RunConfig.compile(cfg(enable_async_research=True, enable_memory=False,
        quality_evaluation_enabled=False, quality_evaluation_min_sources=1, max_react_tool_calls=5))
    team = await build(run_config=run)
    host, factory, marker = await workers(team, store, tmp_path)
    item, contract = assignment()
    await host.prepare(item, contract)
    # 正式 Worker 在领队的 research_supervisor 阶段尚未结束时加入。
    host.recovery.snapshot.inflight = "research_supervisor"
    await host.recovery.save(host.recovery.snapshot)
    events = []

    class Runtime:
        settings = SimpleNamespace(is_demo=False)

        async def create_recovery_store(self):
            return store

        async def bind_research_team(self, recovery):
            assert recovery.lease == team.lease
            return team

        async def aclose(self):
            events.append("closed")

    async def create_runtime(settings):
        return Runtime()

    async def authorize(owner, application):
        return research_principal(owner)

    @asynccontextmanager
    async def ports(run, config, recovery):
        yield RunResources(factory, host.researcher.tools_for)

    monkeypatch.setattr(team_executor.ASRuntime, "create", create_runtime)
    monkeypatch.setattr(team_executor, "authorize_run_owner", authorize)
    monkeypatch.setattr(
        team_executor, "production_resources", lambda *args, **kwargs: ports
    )
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    result = await team_executor.execute(
        {"lease": asdict(team.lease), "task_id": item.task_id}
    )
    assert result.assessment["handoff"]["accepted"]
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1
    async with store.transaction(team.lease):
        pass
    assert events == ["closed"]
    persisted, _ = await store.load(team.lease.run_id, team.lease.user_id)
    assert persisted.inflight == "research_supervisor"
    # The same formal invocation reuses the SQL artifact instead of model/tool execution.
    await team_executor.execute({"lease": asdict(team.lease), "task_id": item.task_id})
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1
    await host.aclose()
