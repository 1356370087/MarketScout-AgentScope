"""Regression tests for the sandbox review security boundaries.

Calls cross the real task authentication and governed execution paths. All credentials are
fixtures. Network access is restricted to an in-process loopback MCP fixture.
"""

import asyncio
import base64
import sys
import time
from pathlib import Path
from types import MethodType, SimpleNamespace

import httpx
import pytest
from aiohttp import web
from mcp.types import Tool as McpDescriptor
from pydantic import BaseModel

from open_deep_research.agentscope_runtime import mcp, sandbox_catalog
from open_deep_research.configuration import Configuration
from open_deep_research.sandbox import egress_proxy, local_provider
from open_deep_research.sandbox.approvals import SecurityApprovalStore
from open_deep_research.sandbox.crypto import encode_task_token
from open_deep_research.sandbox.egress_proxy import GatewayEgressProxy
from open_deep_research.sandbox.gateway import (
    GatewayRunContext,
    GatewayRuntime,
    create_gateway_app,
)
from open_deep_research.sandbox.gateway_catalog import load_gateway_catalog_tools
from open_deep_research.sandbox.schema import (
    filesystem_path_allowed,
    load_policy_bundle,
    policy_digest,
)
from open_deep_research.sandbox.wire import TaskTokenClaimsV1
from open_deep_research.tools import governance
from open_deep_research.tools.base import (
    ToolEffect,
    ToolExecutionZone,
    ToolOrigin,
    ToolResult,
    build_tool,
)

ROOT_KEY = base64.b64encode(b"sandbox-review-fixture-key-32-bytes").decode()


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "SANDBOX_TASK_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    recorder = SimpleNamespace(active_span=lambda: SimpleNamespace(record_outcome=lambda **kw: None, score=lambda *a: None))
    monkeypatch.setattr(governance, "get_trace_recorder", lambda _: recorder)


def fixture_config(tmp_path, monkeypatch, *, offline=False, **settings):
    policy = Path("config/sandbox-policy.toml").read_text(encoding="utf-8")
    policy = policy.replace("allow_domains = []", 'allow_domains = ["allowed.example"]', 1)
    if offline:
        policy = policy.replace('mode = "gateway-only"', 'mode = "offline"', 1)
    path = tmp_path / "policy.toml"
    path.write_text(policy, encoding="utf-8")
    cfg = Configuration(
        sandbox_enabled=True, enable_async_research=True,
        sandbox_root_signing_key=ROOT_KEY, sandbox_policy_path=str(path),
        event_log_enabled=False, sqlite_observability_enabled=False,
        token_usage_accounting_enabled=False, **settings,
    )
    monkeypatch.setattr(Configuration, "from_runnable_config", classmethod(lambda cls, _=None: cfg))
    return cfg


class FixtureInternal:
    def signed(self, model, **values):
        return model(service_timestamp=time.time(), service_nonce="fixture-internal-nonce", service_signature="fixture", **values)

    async def post(self, path, request):
        if path.endswith("target/check"):
            return {"decision": None, "version": 0, "approvals": []}
        if path.endswith("mode/get"):
            return {"mode": None}
        return {"status": "reserved"}


def fixture_runtime(cfg, **configurable):
    runtime = GatewayRuntime(cfg)
    runtime.internal = FixtureInternal()
    config = {"configurable": {**cfg.model_dump(mode="json"), **configurable}, "metadata": {}}
    runtime.runs["review-run"] = GatewayRunContext(config=config, fence_token=1, expires_at=time.time() + 300)
    # Keep real authentication and execution. Replace only resource assembly;
    # tests inject static tools instead of discovering unrelated providers.
    runtime.invoke_tool = MethodType(GatewayRuntime.invoke_tool.__wrapped__, runtime)
    runtime.tool_catalog = MethodType(GatewayRuntime.tool_catalog.__wrapped__, runtime)
    return runtime


def task_headers(runtime, nonce, *, task_id="researcher-task"):
    cfg = runtime.configurable
    claims = TaskTokenClaimsV1(
        run_id="review-run", task_id=task_id, fence_token=1,
        profile_id="research-gateway-only",
        policy_digest=policy_digest(load_policy_bundle(cfg.sandbox_policy_path)),
        issued_at=time.time() - 1, expires_at=time.time() + 120, jti="fixture-task-token",
    )
    return {"Authorization": "Bearer " + encode_task_token(claims, runtime.keys.task_token),
            "X-Sandbox-Timestamp": str(time.time()), "X-Sandbox-Nonce": nonce}


def tool_request(name, *, role="researcher", arguments=None, operation="fixture-op", task_id="researcher-task"):
    return {"run_id": "review-run", "task_id": task_id, "role": role,
            "stage": "researching", "execution_zone": "gateway", "tool_name": name,
            "arguments": arguments or {}, "tool_call_id": operation,
            "logical_operation_id": operation}


class EmptyInput(BaseModel):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_tool_execution_matches_catalog_whitelist(tmp_path, monkeypatch, allowed):
    cfg = fixture_config(tmp_path, monkeypatch, researcher_tool_whitelist=["hidden_probe" if allowed else "public_probe"])
    runtime = fixture_runtime(cfg)
    calls = []

    async def call(input, context, on_progress=None):
        calls.append(context.role)
        return ToolResult(output="hidden-tool-executed")

    tool = build_tool(name="hidden_probe", description="Review fixture", input_schema=EmptyInput,
                      call=call, origin=ToolOrigin.SYSTEM, effect=ToolEffect.READ_ONLY,
                      execution_zone=ToolExecutionZone.GATEWAY)

    async def assembled(role, config):
        return [tool]

    monkeypatch.setattr(sandbox_catalog, "assembled_tools", assembled)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(create_gateway_app(runtime)), base_url="http://review") as client:
        catalog = await client.post("/v1/tools/catalog", json={"run_id": "review-run", "task_id": "researcher-task", "role": "researcher"}, headers=task_headers(runtime, "fixture-catalog-nonce"))
        assert catalog.status_code == 200
        assert [row["name"] for row in catalog.json()["tools"]] == ([tool.name] if allowed else [])
        response = await client.post("/v1/tools/call", json=tool_request(tool.name), headers=task_headers(runtime, "fixture-hidden-tool-nonce"))
    assert response.status_code == 200
    assert response.json()["status"] == ("completed" if allowed else "failed")
    if not allowed:
        assert response.json()["error"]["error_type"] == "permission_denied"
    assert calls == (["researcher"] if allowed else [])


@pytest.mark.asyncio
async def test_worker_cannot_forge_supervisor_to_skip_sensitive_call_approval(tmp_path, monkeypatch):
    cfg = fixture_config(tmp_path, monkeypatch, require_sensitive_tool_approval=True)
    runtime = fixture_runtime(cfg)
    bundle = load_policy_bundle(cfg.sandbox_policy_path)
    profile = bundle.profiles["research-gateway-only"].model_copy(deep=True)
    profile.tools.allow_tools = ["sensitive_probe"]
    monkeypatch.setattr("open_deep_research.sandbox.gateway.resolve_profile", lambda _: (bundle, "research-gateway-only", profile))
    calls = []

    async def call(input, context, on_progress=None):
        calls.append(context.role)
        return ToolResult(output="fixture-sensitive-read")

    tool = build_tool(name="sensitive_probe", description="Review fixture", input_schema=EmptyInput,
                      call=call, origin=ToolOrigin.SYSTEM, effect=ToolEffect.SENSITIVE_READ,
                      execution_zone=ToolExecutionZone.GATEWAY)

    async def assembled(role, config):
        return [tool]

    monkeypatch.setattr(sandbox_catalog, "assembled_tools", assembled)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(create_gateway_app(runtime)), base_url="http://review") as client:
        denied = await client.post("/v1/tools/call", json=tool_request(tool.name, operation="researcher-probe"), headers=task_headers(runtime, "fixture-researcher-nonce"))
        forged = await client.post("/v1/tools/call", json=tool_request(tool.name, role="supervisor", operation="supervisor-probe"), headers=task_headers(runtime, "fixture-supervisor-nonce"))
        trusted = await client.post("/v1/tools/call", json=tool_request(tool.name, role="supervisor", operation="trusted-supervisor-probe", task_id="supervisor"), headers=task_headers(runtime, "fixture-trusted-supervisor-nonce", task_id="supervisor"))
    assert denied.json()["error"]["error_type"] == "sensitive_tool_approval_required"
    assert forged.status_code == 401
    assert forged.json()["detail"] == "sandbox_task_tool_role_mismatch"
    assert trusted.json()["status"] == "completed"
    assert calls == ["supervisor"]


def test_filesystem_roots_and_absolute_denies_restrict_subdirectories():
    profile = load_policy_bundle("config/sandbox-policy.toml").profiles["developer-workspace"].model_copy(deep=True)
    profile.filesystem.read_roots = ["/workspace/work/public"]
    profile.filesystem.write_roots = ["/workspace/work/public"]
    assert not filesystem_path_allowed(profile, "private/secret.txt", write=False)
    assert not filesystem_path_allowed(profile, "private/data.txt", write=True)
    assert filesystem_path_allowed(profile, "public/data.txt", write=False)
    assert filesystem_path_allowed(profile, "public/data.txt", write=True)
    assert not filesystem_path_allowed(profile, "publicity/data.txt", write=True)
    assert not filesystem_path_allowed(profile, " public/data.txt", write=True)
    assert filesystem_path_allowed(profile, "public\\data.txt", write=True)
    profile.filesystem.read_roots = ["/workspace/work"]
    profile.filesystem.deny_read = ["/workspace/work/private/*"]
    assert not filesystem_path_allowed(profile, "private/secret.txt", write=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["permission", "offline", "private_host"])
async def test_mcp_discovery_does_not_send_credentials_before_authorization(tmp_path, monkeypatch, reason):
    requests = []

    async def handler(request):
        requests.append((request.path, request.headers.get("Authorization")))
        return web.Response(status=403, text="fixture stops MCP discovery")

    app = web.Application()
    app.router.add_route("*", "/mcp", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    cfg = fixture_config(tmp_path, monkeypatch, offline=reason == "offline",
                         allowed_mcp_servers=[endpoint],
                         mcp_config={"url": endpoint, "tools": ["fixture_probe"],
                                     "tool_effects": {"fixture_probe": "read_only"}, "auth_required": True})
    permissions = [] if reason == "permission" else ["research.tool.mcp"]
    config = {"configurable": {**cfg.model_dump(mode="json"),
                               "langgraph_auth_user": {"roles": ["researcher"], "permissions": permissions},
                               "_sandbox_credential_vault": {"mcp_tokens": {"access_token": "review-fixture-token"}}},
              "metadata": {"deployment_surface": "http", "sandbox_gateway_physical": True}}
    try:
        tools = await asyncio.wait_for(mcp.load_native_mcp_tools(config, set()), 10)
        assert tools == []
        assert requests == []
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("url,offline,allowed", [("file:///review-private/sample.txt", True, False), ("http://allowed.example:8088/private", False, False), ("https://allowed.example/public", False, True)])
@pytest.mark.parametrize("transport", ["stdio", "streamable_http"])
@pytest.mark.asyncio
async def test_browser_target_scheme_and_port_are_governed(tmp_path, monkeypatch, url, offline, allowed, transport):
    endpoint = None if transport == "stdio" else "https://allowed.example"
    cfg = fixture_config(tmp_path, monkeypatch, offline=offline and transport == "stdio",
                         browser_mcp_enabled=True, allow_http_stdio_mcp=True,
                         allowed_mcp_servers=[endpoint] if endpoint else [],
                         browser_mcp_config={"transport": transport, "url": endpoint, "tools": ["browser_navigate"],
                                             "tool_effects": {"browser_navigate": "read_only"}})
    calls = []

    async def ensure():
        pass

    async def get_tool(name):
        async def handle(**kwargs):
            calls.append(kwargs["url"])
            return SimpleNamespace(state=None, content=[SimpleNamespace(text="fixture browser page")])
        return handle

    server = SimpleNamespace(ensure=ensure, client=SimpleNamespace(get_tool=get_tool))
    tool = mcp.NativeMcpTool(server, McpDescriptor(name="browser_navigate", description="Navigate fixture",
                              inputSchema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}),
                             origin=ToolOrigin.BROWSER, effect=ToolEffect.READ_ONLY,
                             retryable=False, egress_urls_url=endpoint)
    assert url in tool.egress_urls({"url": url})

    async def public_endpoint(value):
        assert value in {endpoint, url}
        return value

    monkeypatch.setattr("open_deep_research.security.network.validate_public_http_url", public_endpoint)
    runtime = fixture_runtime(cfg, langgraph_auth_user={"roles": ["researcher"], "permissions": ["research.tool.browser"]})

    async def assembled(role, config):
        return [tool]

    monkeypatch.setattr(sandbox_catalog, "assembled_tools", assembled)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(create_gateway_app(runtime)), base_url="http://review") as client:
        response = await client.post("/v1/tools/call", json=tool_request(tool.name, arguments={"url": url}), headers=task_headers(runtime, "fixture-browser-nonce"))
    assert response.json()["status"] == ("completed" if allowed else "failed"), response.text
    assert calls == ([url] if allowed else [])


class MemoryWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow_once", "allow_run"])
async def test_gateway_mcp_discovery_waits_for_approval_and_honors_revocation(tmp_path, monkeypatch, decision):
    endpoint = "https://mcp-review.example"
    cfg = fixture_config(tmp_path, monkeypatch, allowed_mcp_servers=[endpoint],
                         mcp_config={"url": endpoint, "tools": ["echo"],
                                     "tool_effects": {"echo": "read_only"}, "auth_required": True})
    runtime = fixture_runtime(cfg, langgraph_auth_user={"roles": ["researcher"], "permissions": ["research.tool.mcp"]},
                              _sandbox_credential_vault={"mcp_tokens": {"access_token": "fixture-review-token"}})
    context = runtime.runs["review-run"]
    context.config["metadata"] = {"deployment_surface": "http", "sandbox_gateway_physical": True}
    runtime.tool_catalog = MethodType(GatewayRuntime.tool_catalog, runtime)
    store = SecurityApprovalStore("review-run", runs_dir=str(tmp_path))
    discoveries = []

    class Authority(FixtureInternal):
        async def post(self, path, request):
            if path.endswith("target/check"):
                return store.check_target(request.capability, request.target, request.fence_token)
            if path.endswith("approvals/wait"):
                version, approvals = store.list()
                return {"version": version, "approvals": [row.model_dump() for row in approvals]}
            if path.endswith("approvals/request"):
                return store.request(task_id=request.task_id, fence_token=request.fence_token,
                    kind=request.kind, capability=request.capability, target=request.target,
                    operation_id=request.operation_id, expires_at=request.expires_at).model_dump()
            if path.endswith("approvals/consume"):
                return store.consume(request.approval_id, operation_id=request.operation_id,
                                     expected_fence_token=request.fence_token).model_dump()
            return await super().post(path, request)

    async def discover(connection):
        discoveries.append(connection)
        return mcp.NativeMcpServer(connection, name="approval-fixture"), [McpDescriptor(name="echo", inputSchema={"type": "object"})]

    runtime.internal = Authority()
    monkeypatch.setattr(mcp, "_discover_via_server", discover)
    payload = {"run_id": "review-run", "task_id": "researcher-task", "role": "researcher"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(create_gateway_app(runtime)), base_url="http://review") as client:
        pending = await client.post("/v1/tools/catalog", json=payload, headers=task_headers(runtime, "mcp-pending-nonce"))
        assert pending.json()["status"] == "approval_required", pending.text
        assert discoveries == []
        store.resolve(pending.json()["approval_id"], decision=decision, actor="reviewer", reason="fixture",
                      expected_fence_token=1)
        accepted = await client.post("/v1/tools/catalog", json=payload, headers=task_headers(runtime, "mcp-accepted-nonce"))
        assert accepted.json()["status"] == "completed", accepted.text
        assert any(tool["name"] == "echo" for tool in accepted.json()["tools"])
        assert len(discoveries) == 1
        assert discoveries[0]["headers"] == {"Authorization": "Bearer fixture-review-token"}
        target = store.target_state(1)["targets"][0]
        store.decide_target(target["target_id"], decision="revoke", reason="fixture", actor="reviewer",
                            expected_version=target["version"], fence_token=1)
        revoked = await client.post("/v1/tools/catalog", json=payload, headers=task_headers(runtime, "mcp-revoked-nonce"))
        assert revoked.json()["status"] == "approval_required", revoked.text
        assert len(discoveries) == 1


@pytest.mark.asyncio
async def test_catalog_wait_refreshes_replay_headers_before_binding_tools(monkeypatch):
    from pydantic import SecretStr

    from open_deep_research.sandbox import gateway_catalog

    requests = []
    original_client = httpx.AsyncClient

    async def reply(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"status": "approval_required", "approval_id": "fixture", "tools": []})
        return httpx.Response(200, json={"tools": []})

    monkeypatch.setattr(gateway_catalog.httpx, "AsyncClient", lambda **kwargs:
                        original_client(transport=httpx.MockTransport(reply), **kwargs))
    assert await load_gateway_catalog_tools("researcher", {"metadata": {"run_id": "r", "task_id": "t"}}, set(),
                                           gateway_url="http://fixture", task_token=SecretStr("fixture-token")) == []
    assert len(requests) == 2
    assert requests[0].headers["X-Sandbox-Nonce"] != requests[1].headers["X-Sandbox-Nonce"]


@pytest.mark.parametrize("connect", [False, True])
@pytest.mark.asyncio
async def test_get_only_proxy_does_not_forward_post_payload(tmp_path, monkeypatch, connect):
    cfg = fixture_config(tmp_path, monkeypatch)
    runtime = fixture_runtime(cfg)
    proxy = GatewayEgressProxy(runtime)

    async def resolve(*args):
        return "8.8.8.8"

    proxy._resolve = resolve
    upstream = MemoryWriter()

    async def open_connection(host, port):
        assert host == "8.8.8.8"
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, upstream

    monkeypatch.setattr(egress_proxy.asyncio, "open_connection", open_connection)
    headers = task_headers(runtime, "fixture-proxy-nonce")
    first = "CONNECT allowed.example:443 HTTP/1.1" if connect else "GET http://allowed.example/ HTTP/1.1"
    request = (first + "\r\nHost: allowed.example\r\nProxy-Authorization: " + headers["Authorization"] +
               "\r\nX-Sandbox-Timestamp: " + headers["X-Sandbox-Timestamp"] +
               "\r\nX-Sandbox-Nonce: " + headers["X-Sandbox-Nonce"] + "\r\n\r\n" +
               "POST /fixture-write HTTP/1.1\r\nHost: allowed.example\r\nContent-Length: 1\r\n\r\nx").encode()
    reader = asyncio.StreamReader()
    reader.feed_data(request)
    reader.feed_eof()
    await proxy.handle(reader, MemoryWriter())
    assert b"POST /fixture-write HTTP/1.1" not in upstream.data
    if connect:
        assert not upstream.data
    else:
        assert bytes(upstream.data).startswith(b"GET / HTTP/1.1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_line", "extra_header", "allowed"),
    [
        ("GET http://allowed.example/ HTTP/1.1", "X-Note: plain-value", True),
        ("GET http://allowed.example/ HTTP/1.1", "X-Note: before\tafter", True),
        ("GET http://allowed.example/ HTTP/1.1", "X-Inject: x\n\nPOST /fixture-write HTTP/1.1\nHost: allowed.example\nContent-Length: 0", False),
        ("GET http://allowed.example/ HTTP/1.1", "X-Inject: x\rhidden", False),
        ("GET http://allowed.example/ HTTP/1.1", "X-Inject: x\x00hidden", False),
        ("GET http://allowed.example/ HTTP/1.1", "X-Inject: x\x7fhidden", False),
        ("GET http://allowed.example/ HTTP/1.1", "Bad Header: value", False),
        ("GET http://allowed.example/ HTTP/1.1", "X@Invalid: value", False),
        ("GET http://allowed.example/ HTTP/1.1", "missing-colon", False),
        ("GET http://allowed.example/ HTTP/1.1\nPOST /fixture-write HTTP/1.1", "X-Note: value", False),
        ("GET http://allowed.example/ HTTP/1.1\x00", "X-Note: value", False),
    ],
)
async def test_proxy_validates_headers_before_authorization_or_dial(
    tmp_path, monkeypatch, request_line, extra_header, allowed,
):
    cfg = fixture_config(tmp_path, monkeypatch)
    runtime = fixture_runtime(cfg)
    proxy = GatewayEgressProxy(runtime)
    authorized = []
    dialed = []
    original_authorize = proxy._authorize
    upstream = MemoryWriter()

    async def authorize(**kwargs):
        authorized.append(kwargs["method"])
        return await original_authorize(**kwargs)

    async def resolve(*args):
        return "8.8.8.8"

    async def open_connection(host, port):
        dialed.append((host, port))
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, upstream

    proxy._authorize = authorize
    proxy._resolve = resolve
    monkeypatch.setattr(egress_proxy.asyncio, "open_connection", open_connection)
    headers = task_headers(runtime, "fixture-header-framing-nonce")
    raw = (request_line + "\r\nProxy-Authorization: " + headers["Authorization"] +
           "\r\nX-Sandbox-Timestamp: " + headers["X-Sandbox-Timestamp"] +
           "\r\nX-Sandbox-Nonce: " + headers["X-Sandbox-Nonce"] +
           "\r\n" + extra_header + "\r\n\r\n").encode("iso-8859-1")
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    downstream = MemoryWriter()
    await proxy.handle(reader, downstream)
    assert authorized == (["GET"] if allowed else [])
    assert dialed == ([("8.8.8.8", 80)] if allowed else [])
    if allowed:
        assert bytes(upstream.data).startswith(b"GET / HTTP/1.1\r\n")
        assert extra_header.lower().encode() in bytes(upstream.data)
    else:
        assert not upstream.data
        assert bytes(downstream.data).startswith(b"HTTP/1.1 403 Forbidden\r\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_stopped_shell_reaps_its_process(tmp_path, monkeypatch, stop):
    # Exercise the real subprocess/cancellation code. Replace Linux bwrap argv
    # with a harmless Python sleeper so the probe also runs on Windows.
    monkeypatch.setattr(local_provider, "task_workspace", lambda _: tmp_path)
    monkeypatch.setattr(local_provider, "safe_workspace_path", lambda *_: tmp_path)
    monkeypatch.setattr(local_provider.shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(local_provider.BubblewrapSandboxProvider, "_build_bubblewrap_argv",
                        staticmethod(lambda **_: [sys.executable, "-c", "import time; time.sleep(30)"]))
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(local_provider.asyncio, "create_subprocess_exec", spawn)
    operation = asyncio.create_task(local_provider.BubblewrapSandboxProvider().run("fixture", config={}, timeout_seconds=1))
    try:
        try:
            await asyncio.wait_for(started.wait(), 5)
        except TimeoutError:
            if operation.done():
                await operation
            raise
        if stop == "cancel":
            operation.cancel()
        with pytest.raises(asyncio.CancelledError if stop == "cancel" else TimeoutError):
            await operation
        await asyncio.sleep(0.05)
        assert processes[0].returncode is not None
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
