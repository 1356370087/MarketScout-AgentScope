"""Native ChatService, PostgreSQL persistence and research middleware integration."""

import pytest
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage._model._agent import AgentData, AgentRecord
from agentscope.app.storage._model._session import ChatModelConfig, SessionConfig
from agentscope.credential import OpenAICredential
from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.message import UserMsg
from test_research_migration import Stages, pipeline

from open_deep_research.agentscope_runtime.app import ASRuntime
from open_deep_research.agentscope_runtime.settings import ASRuntimeSettings

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("pause", [None, "plan_approval"])
async def test_pg_chat_service_runs_and_persists_research(pg_url, pause):
    runtime = await ASRuntime.create(
        ASRuntimeSettings(
            database_url=pg_url,
            database_schema="agentscope_runtime",
            storage_auto_create=True,
            bus_table_prefix="as_m5_service_",
        )
    )
    flow = pipeline(Stages(pause))
    resolved = []

    async def resolve(user, agent, session, workspace):
        assert user == "research-owner"
        resolved.append((user, agent, session))
        return flow

    app = runtime.build_app(
        extra_agent_middlewares=runtime.research_middleware_factory(resolve)
    )
    try:
        async with app.router.lifespan_context(app):
            data = AgentData(
                name="research",
                context_config=ContextConfig(),
                react_config=ReActConfig(),
            )
            agent_id = await runtime.storage.upsert_agent(
                "research-owner", AgentRecord(user_id="research-owner", data=data)
            )
            credential = await runtime.storage.upsert_credential(
                "research-owner",
                OpenAICredential(api_key="fixture", base_url="http://127.0.0.1:1/v1"),
            )
            session = await runtime.storage.upsert_session(
                user_id="research-owner",
                agent_id=agent_id,
                config=SessionConfig(
                    name="M5 acceptance",
                    workspace_id="m5-acceptance",
                    chat_model_config=ChatModelConfig(
                        type="OpenAIChatModel",
                        credential_id=credential,
                        model="fixture",
                        parameters={},
                    ),
                ),
            )
            await app.state.chat_service.run(
                "research-owner", session.id, agent_id, UserMsg("user", "研究市场")
            )
            stored = await runtime.storage.get_session(
                "research-owner", agent_id, session.id
            )
            assert resolved == [("research-owner", agent_id, session.id)]
            if pause:
                assert stored.state.middle_context["research"]["status"] == "waiting"
                request = flow._pending_event()
                await app.state.chat_service.run(
                    "research-owner",
                    session.id,
                    agent_id,
                    UserConfirmResultEvent(
                        reply_id=flow.state.reply_id,
                        confirm_results=[
                            ConfirmResult(
                                confirmed=True, tool_call=request.tool_calls[0]
                            )
                        ],
                    ),
                )
                stored = await runtime.storage.get_session(
                    "research-owner", agent_id, session.id
                )
            assert stored.state.middle_context["research"]["status"] == "completed"
            assert flow.state.final_report == "report"
            await app.state.chat_service.run(
                "other-user", session.id, agent_id, UserMsg("user", "forged")
            )
            assert len(resolved) == (2 if pause else 1)
    finally:
        await runtime.aclose()


async def test_pg_service_rebuilds_durable_pipeline_and_consumes_approval(pg_url):
    from open_deep_research.agentscope_runtime.recovery import (
        RecoverySession,
        RecoveryStages,
    )
    from open_deep_research.agentscope_runtime.research_pipeline import (
        ResearchPipeline,
        ResearchSnapshot,
    )

    runtime = await ASRuntime.create(
        ASRuntimeSettings(
            database_url=pg_url,
            database_schema="agentscope_runtime",
            storage_auto_create=True,
            bus_table_prefix="as_m6_service_",
        )
    )
    recovery_store = await runtime.create_recovery_store()
    opened = []

    async def resolve(user, agent, session_id, workspace):
        session = await RecoverySession.open(recovery_store, session_id, user)
        opened.append(session.lease.fence)
        flow = ResearchPipeline(
            session.snapshot,
            RecoveryStages(Stages("plan_approval"), session),
            session.save,
            config_fingerprint="frozen",
            recovery=session,
        )
        await session.consume_decisions(flow)
        return flow

    app = runtime.build_app(
        extra_agent_middlewares=runtime.research_middleware_factory(resolve)
    )
    try:
        async with app.router.lifespan_context(app):
            data = AgentData(
                name="durable",
                context_config=ContextConfig(),
                react_config=ReActConfig(),
            )
            agent_id = await runtime.storage.upsert_agent(
                "owner", AgentRecord(user_id="owner", data=data)
            )
            credential = await runtime.storage.upsert_credential(
                "owner",
                OpenAICredential(api_key="fixture", base_url="http://127.0.0.1:1/v1"),
            )
            session = await runtime.storage.upsert_session(
                user_id="owner",
                agent_id=agent_id,
                config=SessionConfig(
                    name="M6",
                    workspace_id="m6-acceptance",
                    chat_model_config=ChatModelConfig(
                        type="OpenAIChatModel",
                        credential_id=credential,
                        model="fixture",
                        parameters={},
                    ),
                ),
            )
            await recovery_store.create_run(
                "owner",
                ResearchSnapshot(run_id=session.id, config_fingerprint="frozen"),
            )
            await app.state.chat_service.run(
                "owner", session.id, agent_id, UserMsg("user", "q")
            )
            state, _ = await recovery_store.load(session.id, "owner")
            assert state.status == "waiting"
            from agentscope.message import ToolCallBlock

            confirmation = UserConfirmResultEvent(
                reply_id=state.reply_id,
                confirm_results=[
                    ConfirmResult(
                        confirmed=True,
                        tool_call=ToolCallBlock(
                            id=state.pending.id, name="plan_approval", input="{}"
                        ),
                    )
                ],
            )
            await app.state.chat_service.run(
                "owner", session.id, agent_id, confirmation
            )
            state, _ = await recovery_store.load(session.id, "owner")
            assert state.status == "completed"
            assert opened == [1, 2]
            stored = await runtime.storage.get_session("owner", agent_id, session.id)
            assert stored.state.middle_context["research"]["status"] == "completed"
    finally:
        await runtime.aclose()
