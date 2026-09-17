"""Native SQL ownership through the shared server's public sandbox routes."""

import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from open_deep_research import server
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.sandbox.approvals import SecurityApprovalStore
from security.rbac.dependencies import get_current_principal
from security.rbac.permissions import (
    RESEARCH_SECURITY_APPROVAL_READ_OWN,
    RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN,
)
from tests.auth_helpers import research_principal


@pytest.mark.asyncio
async def test_native_public_approval_owner_resolution_and_usage(tmp_path, monkeypatch):
    from security.rbac import dependencies

    @asynccontextmanager
    async def identity_session():
        # Principal is injected; the ownership bridge uses the actual recovery SQL.
        yield None

    monkeypatch.setattr(dependencies, "session_scope", identity_session)
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "native.db").as_posix())
    await store.create_tables()
    run = RunConfig.compile({"configurable": {"runs_dir": str(tmp_path)}})
    await store.create_from_config(
        "alice", "native-public", run, application={"configuration": run.snapshot()}
    )
    lease = await store.acquire("native-public", "alice")
    monkeypatch.setattr(
        server,
        "_native_research_service",
        SimpleNamespace(store=store, runs_dir=tmp_path),
    )
    principal = replace(
        research_principal("alice"),
        permissions=frozenset(
            {
                *research_principal("alice").permissions,
                RESEARCH_SECURITY_APPROVAL_READ_OWN.code,
                RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN.code,
            }
        ),
    )
    server.app.dependency_overrides[get_current_principal] = lambda: principal
    approval = SecurityApprovalStore("native-public", runs_dir=str(tmp_path)).request(
        task_id="task",
        fence_token=lease.fence,
        kind="network",
        capability="network",
        target={"domain": "example.org", "port": 443},
        operation_id="op",
        expires_at=time.time() + 300,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(server.app), base_url="http://api"
        ) as client:
            listed = await client.get("/runs/native-public/security-approvals")
            assert listed.status_code == 200, listed.text
            assert listed.json()["approvals"][0]["approval_id"] == approval.approval_id
            resolved = await client.post(
                f"/runs/native-public/security-approvals/{approval.approval_id}",
                json={"decision": "allow_once"},
            )
            assert resolved.status_code == 200, resolved.text
            assert any(
                e["payload"]["type"] == "research.public"
                for e in await store.events("native-public", "alice")
            )
            usage = await client.get("/runs/native-public/usage")
            assert (
                usage.status_code == 200
                and usage.json()["accounting_status"] == "complete"
            )
            principal = replace(principal, user_id="bob")
            assert (
                await client.get("/runs/native-public/security-approvals")
            ).status_code == 404
    finally:
        server.app.dependency_overrides.pop(get_current_principal, None)
        await store.release(lease)
        await store.aclose()
