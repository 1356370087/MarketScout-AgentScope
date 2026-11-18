"""Additional plan allowance shares one physical personal charge."""

from unittest.mock import AsyncMock

import pytest

from open_deep_research.platform import accounting, runtime, quota, integration


@pytest.mark.asyncio
async def test_plan_denial_releases_personal_reservation_before_provider_call(monkeypatch):
    monkeypatch.setattr(runtime, "enabled", lambda: True)
    monkeypatch.setattr(runtime, "snapshot", lambda: {"document": {}})
    monkeypatch.setattr(integration, "check_credentials", AsyncMock())
    reserved, released = AsyncMock(), AsyncMock()
    monkeypatch.setattr(quota, "reserve", reserved)
    monkeypatch.setattr(quota, "release", released)
    port = type("Port", (), {"reserve": AsyncMock(side_effect=quota.QuotaExceeded())})()
    with accounting.scoped_service_budget(port):
        with pytest.raises(quota.QuotaExceeded):
            await accounting.begin("test", "prompt", 10, "test", identity="one-charge", owner="platform", pricing=(1, 1))
    reserved.assert_awaited_once()
    released.assert_awaited_once_with("one-charge")


@pytest.mark.asyncio
async def test_unknown_and_completed_calls_share_existing_receipt(monkeypatch):
    settled, unknown = AsyncMock(), AsyncMock()
    monkeypatch.setattr(quota, "settle", settled)
    monkeypatch.setattr(quota, "mark_unknown", unknown)
    port = type("Port", (), {"settle": AsyncMock()})()
    charge = accounting.ServiceCharge("physical-call", (1, 2), port)
    await accounting.complete(charge)
    unknown.assert_awaited_once_with("physical-call")
    port.settle.assert_awaited_with("physical-call", None)
    await accounting.complete(charge, {"input_tokens": 5, "output_tokens": 10})
    settled.assert_awaited_once_with("physical-call", 25)
    port.settle.assert_awaited_with("physical-call", 25)


@pytest.mark.asyncio
async def test_feishu_application_token_cache_honors_expiry_and_secret_rotation(monkeypatch):
    import httpx
    from types import SimpleNamespace
    from open_deep_research.intelligence import feishu
    clock, requests = [100.0], []
    monkeypatch.setattr(feishu, "_token", None)
    monkeypatch.setattr(feishu, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    config = SimpleNamespace(feishu_app_id="test-app", feishu_app_secret="test-secret")
    def respond(request):
        requests.append(request)
        assert b"grant_type=client_credentials" in request.content
        return httpx.Response(200, json={"access_token": "token-" + str(len(requests)), "expires_in": 2})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await feishu.tenant_token(client, config) == "token-1"
        assert await feishu.tenant_token(client, config) == "token-1"
        clock[0] = 102.0
        assert await feishu.tenant_token(client, config) == "token-2"
        config.feishu_app_secret = "rotated-test-secret"
        assert await feishu.tenant_token(client, config) == "token-3"
    assert len(requests) == 3
