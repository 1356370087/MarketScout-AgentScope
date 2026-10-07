"""Run credentials remain isolated across concurrent Gateway tool operations."""

import asyncio
import base64
import time
from types import SimpleNamespace

import pytest

from open_deep_research.models.credentials_context import (
    bind_run_key,
    current_run_key,
    reset_run_key,
)
from open_deep_research.sandbox.gateway import GatewayRunContext, GatewayRuntime
from open_deep_research.configuration import Configuration


@pytest.mark.asyncio
async def test_tool_credentials_restore_on_success_failure_and_cancellation():
    runtime = GatewayRuntime(Configuration(sandbox_root_signing_key=base64.b64encode(b"k" * 32).decode()))

    async def invoke(request, context):
        await asyncio.sleep(0)  # Allow all three request contexts to overlap.
        assert current_run_key() == context.api_keys["LITELLM_RUN_KEY"]
        if request.mode == "failed":
            raise ValueError("test failure")
        if request.mode == "cancelled":
            raise asyncio.CancelledError
        return "done"

    runtime._invoke_tool = invoke
    parent_token = bind_run_key("parent-context")
    try:
        async def call(mode):
            try:
                request = SimpleNamespace(run_id="credential-run", task_id="task", stage="researching", role="researcher", mode=mode)
                context = GatewayRunContext(config={"configurable": {"search_api": "none", "web_pipeline_mode": "legacy"}},
                                            api_keys={"LITELLM_RUN_KEY": mode}, fence_token=1, expires_at=time.time() + 60)
                return await runtime.invoke_tool(request, context)
            finally:
                assert current_run_key() == "parent-context"

        results = await asyncio.gather(*(call(mode) for mode in ("success", "failed", "cancelled")), return_exceptions=True)
        assert results[0] == "done"
        assert isinstance(results[1], ValueError)
        assert isinstance(results[2], asyncio.CancelledError)
    finally:
        reset_run_key(parent_token)
