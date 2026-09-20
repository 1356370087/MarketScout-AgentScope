"""Official server approval routes use native SQL ownership and fencing."""

import time
from dataclasses import replace

import httpx
import pytest

from open_deep_research import server
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.sandbox.approvals import SecurityApprovalStore
from security.rbac.dependencies import get_current_principal
from security.rbac.permissions import RESEARCH_SECURITY_APPROVAL_READ_OWN, RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN
from tests.auth_helpers import research_principal


@pytest.mark.asyncio
async def test_public_approval_enforces_native_owner_epoch_and_sql_event(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LOCAL_DEV_AUTH_BYPASS", "true")
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    config = RunConfig.compile({"configurable": {"sandbox_egress_approval_mode": "manual"}})
    state = await store.create_from_config("alice", "run", config,
                                          application={"configuration": config.snapshot()})
    state.status = "running"
    lease = await store.acquire("run", "alice")
    await store.save(lease, state)
    approvals = SecurityApprovalStore("run", runs_dir=tmp_path)
    request = approvals.request(task_id="task", fence_token=lease.fence, kind="network", capability="fetch_url",
                                target={"domain": "example.test", "port": 443}, operation_id="fetch-one",
                                expires_at=time.time() + 60)
    service = NativeRuns(store, None, None, runs_dir=tmp_path)
    original_service = server._native_research_service
    server._set_native_research_service(service)
    original_overrides = dict(server.app.dependency_overrides)

    def principal(name):
        value = research_principal(name)
        return replace(value, permissions=value.permissions | {
            RESEARCH_SECURITY_APPROVAL_READ_OWN.code, RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN.code,
        })

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(server.app), base_url="http://test") as client:
            server.app.dependency_overrides[get_current_principal] = lambda: principal("bob")
            assert (await client.get("/runs/run/security-approvals")).status_code == 404
            server.app.dependency_overrides[get_current_principal] = lambda: principal("alice")
            response = await client.get("/runs/run/security-approvals")
            assert response.status_code == 200, response.text
            assert response.json()["approvals"][0]["approval_id"] == request.approval_id
            response = await client.post(f"/runs/run/security-approvals/{request.approval_id}", json={"decision": "allow_once"})
            assert response.status_code == 200, response.text
            assert response.json()["decision"] == "allow_once"
            events = await service.events("run", "alice")
            assert any(event.type == "security.approval.resolved" for event in events)
            await store.release(lease)
            response = await client.post(f"/runs/run/security-approvals/{request.approval_id}", json={"decision": "deny"})
            assert response.status_code == 409 and response.json()["detail"] == "stale_fence"
    finally:
        server.app.dependency_overrides.clear()
        server.app.dependency_overrides.update(original_overrides)
        server._set_native_research_service(original_service)
        await service.aclose()
        await store.aclose()
