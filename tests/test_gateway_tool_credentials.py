"""Run credentials remain isolated across concurrent Gateway tool operations."""

import asyncio
from types import SimpleNamespace

import pytest

from open_deep_research.models.gateway import (
    bind_run_key,
    current_run_key,
    reset_run_key,
)
from open_deep_research.sandbox.gateway import GatewayRuntime


@pytest.mark.asyncio
async def test_tool_credentials_restore_on_success_failure_and_cancellation():
    runtime = object.__new__(GatewayRuntime)

    async def invoke(request, context):
        await asyncio.sleep(0)  # Allow all three request contexts to overlap.
        assert current_run_key() == context.api_keys["LITELLM_RUN_KEY"]
        if request == "failed":
            raise ValueError("test failure")
        if request == "cancelled":
            raise asyncio.CancelledError
        return "done"

    runtime._invoke_tool = invoke
    parent_token = bind_run_key("parent-context")
    try:
        async def call(mode):
            try:
                return await runtime.invoke_tool(mode, SimpleNamespace(api_keys={"LITELLM_RUN_KEY": mode}))
            finally:
                assert current_run_key() == "parent-context"

        results = await asyncio.gather(*(call(mode) for mode in ("success", "failed", "cancelled")), return_exceptions=True)
        assert results[0] == "done"
        assert isinstance(results[1], ValueError)
        assert isinstance(results[2], asyncio.CancelledError)
    finally:
        reset_run_key(parent_token)
