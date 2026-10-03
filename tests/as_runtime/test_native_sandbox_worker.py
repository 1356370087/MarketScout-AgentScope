"""The container Researcher executes an AgentScope loop using task capabilities."""

import json
import sys

import httpx
import pytest
from agentscope.message import UserMsg

from open_deep_research.agentscope_runtime.sandbox_worker import execute_worker
from open_deep_research.quality.contract import build_research_coverage_contract
from open_deep_research.sandbox.wire import SandboxTaskPayloadV1


@pytest.mark.asyncio
async def test_worker_native_model_loop_and_gateway_tool(monkeypatch):
    monkeypatch.setenv("SANDBOX_TASK_TOKEN", "capability")
    monkeypatch.setenv("SANDBOX_GATEWAY_URL", "https://gateway.invalid")
    monkeypatch.setenv("QUALITY_EVALUATION_ENABLED", "false")
    calls, clients = [], []

    def handler(req):
        body = json.loads(req.content)
        calls.append((req.url.path, body))
        assert req.headers["authorization"] == "Bearer capability"
        assert body["run_id"] == "r" and body["task_id"] == "t"
        if req.url.path == "/v1/tools/catalog":
            return httpx.Response(
                200,
                json={
                    "tools": [
                        {
                            "name": "fetch_url",
                            "definition": {
                                "name": "fetch_url",
                                "description": "Fetch",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"url": {"type": "string"}},
                                    "required": ["url"],
                                },
                            },
                            "origin": "system",
                            "effect": "read_only",
                            "retryable": True,
                            "concurrency_safe": True,
                        }
                    ]
                },
            )
        if req.url.path == "/v1/tools/call":
            assert (
                body["tool_name"] == "fetch_url" and body["tool_call_id"] == "fetch-1"
            )
            return httpx.Response(
                200,
                json={
                    "logical_operation_id": body["logical_operation_id"],
                    "tool_call_id": body["tool_call_id"],
                    "status": "completed",
                    "output": {"evidence": [], "text": "Fixture page"},
                },
            )
        role = body["role"]
        if (
            role == "researcher"
            and sum(p == "/v2/models/complete" for p, b in calls) == 1
        ):
            return httpx.Response(
                200,
                json={
                    "logical_operation_id": body["logical_operation_id"],
                    "status": "completed",
                    "requested_model": body["model"],
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "fetch-1",
                                "type": "function",
                                "function": {
                                    "name": "fetch_url",
                                    "arguments": '{"url":"https://fixture.invalid"}',
                                },
                            }
                        ],
                    },
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            )
        return httpx.Response(
            200,
            json={
                "protocol_version": 2,
                "logical_operation_id": body["logical_operation_id"],
                "status": "completed",
                "requested_model": body["model"],
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Research result"
                    if role == "researcher"
                    else "Compressed findings",
                },
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        )

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        instance = original(*args, **kwargs)
        clients.append(instance)
        return instance

    monkeypatch.setattr(httpx, "AsyncClient", client)
    payload = SandboxTaskPayloadV1(
        run_id="r",
        task_id="t",
        research_topic="topic",
        researcher_state={"coverage_contract": build_research_coverage_contract(
            [UserMsg("user", "topic")]
        ).model_dump(mode="json")},
        runtime_config={},
        profile_id="p",
        policy_digest="d",
        fence_token=1,
    )
    result = await execute_worker(
        payload,
        {
            "configurable": {"quality_evaluation_enabled": False},
            "metadata": {"run_id": "r", "task_id": "t"},
        },
    )
    assert result["compressed_research"] == "Compressed findings"
    assert [b["role"] for p, b in calls if p == "/v2/models/complete"] == [
        "researcher",
        "researcher",
        "compression",
    ]
    assert sum(p == "/v1/tools/call" for p, b in calls) == 1
    assert all(c.is_closed for c in clients)
    assert not any(k.startswith("langchain") for k in sys.modules)
