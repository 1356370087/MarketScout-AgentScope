"""Ensure TypeScript protocol samples have not drifted from server serializers."""

from tests.as_runtime.frontend_contract_samples import TARGET, sample_source
import pytest


def test_browser_event_samples_match_current_wire_serializers():
    assert TARGET.read_text(encoding="utf-8") == sample_source()


@pytest.mark.asyncio
async def test_browser_http_samples_match_current_native_routes():
    from tests.as_runtime.frontend_http_samples import TARGET, sample_source

    assert TARGET.read_text(encoding="utf-8") == await sample_source()
