"""Tests for FastAPI authentication behavior."""

import pytest

from security.auth import get_current_user
from security.rbac.principal import Principal


@pytest.mark.asyncio
async def test_local_dev_auth_bypass_returns_fixed_user(monkeypatch):
    """The opt-in local bypass should not require an Authorization header."""
    monkeypatch.setenv("LOCAL_DEV_AUTH_BYPASS", "true")

    user = await get_current_user(None)

    assert isinstance(user, Principal)
    assert user.user_id == "local-dev-user"
    assert user.roles == frozenset({"researcher", "developer"})
    assert user.is_active


@pytest.mark.asyncio
async def test_auth_still_requires_header_by_default(monkeypatch):
    """Authentication must remain fail-closed when the bypass is disabled."""
    monkeypatch.delenv("LOCAL_DEV_AUTH_BYPASS", raising=False)

    with pytest.raises(Exception) as exc_info:
        await get_current_user(None)

    assert getattr(exc_info.value, "status_code", None) == 401
