"""Lost tool replies recover committed receipts without repeating physical effects."""

import json

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from open_deep_research.sandbox import gateway_tool
from open_deep_research.tools.base import ToolContext, ToolOrigin, build_tool
from open_deep_research.tools.governance import ToolOutcomeError


class Input(BaseModel):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lost_reply", "http500"])
@pytest.mark.parametrize("receipt", ["completed", "failed", "missing", "mismatch"])
async def test_tool_transport_only_reads_receipt_after_lost_reply(monkeypatch, failure, receipt):
    requests = []
    def serve(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body, request.headers["X-Sandbox-Nonce"]))
        if request.url.path == "/v1/tools/call":
            if failure == "lost_reply":
                raise httpx.ReadError("reply lost", request=request)
            return httpx.Response(500)
        assert request.url.path == "/v1/tools/lookup"
        if receipt == "missing":
            return httpx.Response(200, json=None, content=b"null")
        return httpx.Response(200, json={
            "logical_operation_id": "different" if receipt == "mismatch" else body["logical_operation_id"],
            "tool_call_id": body["tool_call_id"],
            "status": "failed" if receipt == "failed" else "completed", "output": "recovered evidence",
            "error": {"error_type": "unknown", "message": "known extraction failure"} if receipt == "failed" else None,
        })

    client_type = httpx.AsyncClient
    monkeypatch.setattr(gateway_tool.httpx, "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(serve)))
    monkeypatch.setattr(gateway_tool, "TOOL_RECEIPT_LOOKUP_SECONDS", 0.02)
    tool = build_tool(name="fetch_url", input_schema=Input, description="fetch", call=lambda *_: None,
                      origin=ToolOrigin.SEARCH)
    proxy = gateway_tool.GatewayToolProxy(tool, "http://gateway", SecretStr("test"))
    context = ToolContext(config={"metadata": {"run_id": "r", "task_id": "t"}},
        role="researcher", tool_call_id="c", operation_id="op")
    if receipt == "completed":
        assert (await proxy.call(Input(), context)).output == "recovered evidence"
    elif receipt == "failed":
        with pytest.raises(ToolOutcomeError, match="known extraction failure"):
            await proxy.call(Input(), context)
    elif receipt == "mismatch":
        with pytest.raises(RuntimeError, match="response_mismatch"):
            await proxy.call(Input(), context)
    else:
        with pytest.raises((httpx.ReadError, httpx.HTTPStatusError)):
            await proxy.call(Input(), context)
    assert sum(path == "/v1/tools/call" for path, _, _ in requests) == 1
    assert len(requests) >= 2
    assert all(body == requests[0][1] for _, body, _ in requests)
    assert len({nonce for _, _, nonce in requests}) == len(requests)
