"""Deterministic model transport for real native worker and process tests."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse, StructuredResponse
from pydantic import BaseModel
from test_research_migration import ScriptedModel, cfg, evidence, tool_call
from test_research_quality import Judge

from open_deep_research.agentscope_runtime.model_policy import (
    ModelCallPolicy,
    ModelPolicyMiddleware,
)
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.research_agents import Researcher
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
from open_deep_research.agentscope_runtime.team_worker import TeamWorkers
from open_deep_research.tools.base import (
    ToolExecutionZone,
    ToolOrigin,
    ToolResult,
    build_tool,
)


class Empty(BaseModel):
    pass


class Model(ScriptedModel):
    def __init__(self, factory):
        super().__init__([])
        self.factory = factory

    async def _call_api(self, *args, **kwargs):
        self.calls.append(kwargs)
        if self.factory.slow:
            self.factory.entered.set()
            await asyncio.sleep(30)
        prior = [
            block.name
            for msg in kwargs["messages"]
            for block in msg.get_content_blocks()
            if isinstance(block, ToolCallBlock)
        ]
        if "web_research" not in prior:
            blocks = [tool_call("web_research", "search")]
        elif "TeamSay" not in prior:
            blocks = [
                tool_call("TeamSay", "report", to="lead", content="Evidence ready")
            ]
        else:
            blocks = [tool_call("ResearchComplete", "done")]
        return ChatResponse(content=blocks, is_last=True)

    async def generate_structured_output(self, messages, schema):
        prompt = messages[-1].get_text_content()
        result = await Judge().structured("quality_evaluation", prompt, schema, {})
        return StructuredResponse(content=result.model_dump(mode="json"))


class Factory:
    run = SimpleNamespace(get=lambda name: {})

    def __init__(self, slow=False):
        self.slow = slow
        self.entered = asyncio.Event()
        self.instances = []

    def build(self, role):
        model = Model(self)
        self.instances.append(model)
        return model

    def descriptor(self, role):
        return {"model": "fixture", "max_output_tokens": 2000}

    def policy_middleware(self, role, candidates=None):
        return ModelPolicyMiddleware(
            ModelCallPolicy(candidates or [self.build(role)], circuit_enabled=False)
        )

    async def complete_with_recovery(self, *args, **kwargs):
        return ChatResponse(
            content=[
                TextBlock(
                    text="研究资料显示市场增长，结论对应证据 ev1，仍需要继续核对数据适用范围。"
                    * 12
                )
            ],
            is_last=True,
        )


async def workers(
    team, store, root, *, slow=False, failpoint=None, ttl=1.5, quality_enabled=False
):
    config = cfg(
        quality_evaluation_min_sources=1,
        max_react_tool_calls=5,
        quality_evaluation_enabled=quality_enabled,
    )
    factory = Factory(slow)
    state, _ = await store.load(team.lease.run_id, team.lease.user_id)
    recovery = RecoverySession(store, team.lease, state)
    models = ResearchModels(factory, recovery=recovery)
    marker = Path(root) / "tool-effects.txt"

    async def effect(input, context, progress):
        with marker.open("a", encoding="utf-8") as stream:
            stream.write(context.operation_id + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return ToolResult(output={"evidence": [evidence()]})

    async def tools_for(assignment):
        return [
            build_tool(
                name="web_research",
                input_schema=Empty,
                description="Collect fixture evidence",
                call=effect,
                origin=ToolOrigin.SYSTEM,
                execution_zone=ToolExecutionZone.HOST_CONTROL,
            )
        ]

    researcher = Researcher(models, lambda: config, tools_for, run_id=team.lease.run_id)
    quality = NativeResearchQuality(models, lambda: config)
    host = TeamWorkers(
        team, recovery, researcher, quality, root, ttl=ttl, failpoint=failpoint
    )
    return host, factory, marker


async def child(request_path):
    import asyncpg
    from agentscope.app.storage._sql import AsyncSQLAlchemyStorage

    from open_deep_research.agentscope_runtime.recovery_store import (
        RecoveryStore,
        RunLease,
    )
    from open_deep_research.agentscope_runtime.team import (
        FencedTeamTransport,
        NativeResearchTeam,
        research_member_template,
    )

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    schema = request["schema"]
    kwargs = {"connect_args": {"server_settings": {"search_path": schema}}}
    store = RecoveryStore(request["url"], engine_kwargs=kwargs)
    pool = await asyncpg.create_pool(
        request["url"].replace("postgresql+asyncpg", "postgresql"),
        server_settings={"search_path": schema},
    )
    storage = AsyncSQLAlchemyStorage(
        request["url"], create_tables=False, auto_migrate=False, engine_kwargs=kwargs
    )
    async with storage:
        lease = RunLease(**request["lease"])
        team = NativeResearchTeam(
            storage,
            FencedTeamTransport(pool, lease, recovery_schema=schema),
            leader_agent_id=request["leader_agent_id"],
            leader_session_id=request["leader_session_id"],
            template=research_member_template(),
        )

        async def failpoint(point):
            should_stop = point == request["window"]
            if request["window"] == "tool_committed" and point == "operation_committed":
                async with pool.acquire() as db:
                    should_stop = (
                        await db.fetchval(
                            "SELECT count(*) FROM as_recovery_operations WHERE run_id=$1 AND kind='tool' AND state='committed'",
                            lease.run_id,
                        )
                        > 0
                    )
            if (
                request["window"] == "completion_committed"
                and point == "operation_committed"
            ):
                async with pool.acquire() as db:
                    should_stop = (
                        await db.fetchval(
                            "SELECT count(*) FROM as_recovery_operations WHERE run_id=$1 AND key LIKE '%:tool:done' AND state='committed'",
                            lease.run_id,
                        )
                        > 0
                    )
            if should_stop:
                # Parent performs an actual kill; child never reaches cleanup.
                Path(request["ready"]).write_text(point, encoding="utf-8")
                await asyncio.sleep(120)

        host, _, _ = await workers(
            team, store, request["root"], failpoint=failpoint, ttl=1.5
        )
        await host.execute(request["task_id"])
    await pool.close()
    await store.aclose()


if __name__ == "__main__":
    import sys

    asyncio.run(child(sys.argv[1]))
