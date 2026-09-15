"""AS-T010/AS-T015 装配入口与 IAM 身份覆盖集成测试（.venv 新框架环境）。

覆盖：
- ASRuntime 组合根：PG 模式（隔离 schema 存储 + 持久总线）与演示模式。
- 框架子应用装配 + IAM JWT 覆盖：无凭证 401、伪造 X-User-ID 无效、
  有效 EdDSA access token 通过（身份取自 token 的 sub）。
- 生命周期：aclose 释放总线与存储（无连接泄漏异常）。
"""

from __future__ import annotations

import httpx
import pytest

from open_deep_research.as_runtime.app import ASRuntime
from open_deep_research.as_runtime.settings import ASRuntimeSettings

pytestmark = pytest.mark.asyncio


def _settings(pg_url: str) -> ASRuntimeSettings:
    return ASRuntimeSettings(
        database_url=pg_url,
        database_schema="agentscope_runtime",
        storage_auto_create=True,
        bus_table_prefix="as_bus_app__",
    )


async def test_runtime_pg_mode_identity_override(pg_url: str) -> None:
    runtime = await ASRuntime.create(_settings(pg_url))
    app = runtime.build_app()
    # 组件与请求必须同 loop：ASGITransport + 手动 lifespan（TestClient 自建 loop 会跨 loop）
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://as-runtime"
            ) as client:
                r = await client.get("/sessions/", params={"agent_id": "probe-agent"})
                assert r.status_code == 401, "无 Bearer 应 401（IAM 覆盖生效）"

                r = await client.get(
                    "/sessions/",
                    params={"agent_id": "probe-agent"},
                    headers={"X-User-ID": "attacker"},
                )
                assert r.status_code == 401, "伪造 X-User-ID 不得通过认证"

                from security.rbac.jwt_service import encode_access_token

                token, _ = encode_access_token(
                    subject="probe-user", session_id="sess-1", authz_version=1
                )
                r = await client.get(
                    "/sessions/",
                    params={"agent_id": "probe-agent"},
                    headers={"X-User-ID": "attacker", "Authorization": f"Bearer {token}"},
                )
                assert r.status_code in (200, 404), (
                    f"有效 JWT 应通过认证（404=Agent 不存在，身份已生效）：{r.status_code}"
                )
    finally:
        await runtime.aclose()


async def test_runtime_demo_mode_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AS_DATABASE_URL", raising=False)
    runtime = await ASRuntime.create(ASRuntimeSettings.from_env())
    assert runtime.settings.is_demo
    from agentscope.app.message_bus import InMemoryMessageBus

    assert isinstance(runtime.message_bus, InMemoryMessageBus)
    app = runtime.build_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://as-runtime"
        ) as client:
            r = await client.get("/openapi.json")
            assert r.status_code == 200
    await runtime.aclose()


async def test_runtime_storage_session_roundtrip(pg_url: str) -> None:
    """AS-T011 装配链路：经 ASRuntime 的存储完成 session 状态回程。"""
    from agentscope.app.storage._model._session import SessionConfig
    from agentscope.state import AgentState

    runtime = await ASRuntime.create(_settings(pg_url))
    try:
        record = await runtime.storage.upsert_session(
            user_id="probe-user",
            agent_id="probe-agent",
            config=SessionConfig(workspace_id="probe-ws"),
        )
        await runtime.storage.update_session_state(
            "probe-user",
            "probe-agent",
            record.id,
            AgentState(session_id=record.id, summary="m2"),
        )
        got = await runtime.storage.get_session("probe-user", "probe-agent", record.id)
        assert got is not None and got.state.summary == "m2"
    finally:
        await runtime.aclose()
