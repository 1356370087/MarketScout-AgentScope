"""Authorization regressions across classifiers, authority, and physical fetches."""

import time

import pytest

from open_deep_research.sandbox.approvals import SecurityApprovalStore
from open_deep_research.sandbox.egress_classifier import (
    EgressClassificationEntry,
    EgressClassifier,
    EgressClassifierLimits,
    EgressModelReply,
    classification_fingerprint,
)
from open_deep_research.sandbox.egress_context import egress_authorizer
from open_deep_research.sandbox.egress_ledger_store import EgressClassificationStore
from tests.test_egress_gateway_wiring import (
    FakeInternal,
    _precheck,
    _profile,
    _runtime_with_run,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["host", "port", "capability", "intent"])
async def test_auto_grants_are_exact(change):
    classifier = EgressClassifier(EgressClassifierLimits())
    calls = []

    async def invoke(call):
        calls.append(call)
        return EgressModelReply(status="completed", content="allow")

    args = dict(host="one.github.io", port=443, capability="tool.egress", intent="research",
                tool_name="fetch_url", invoker=invoke)
    await classifier.classify_target(**args)
    cached = await classifier.classify_target(**args)
    assert cached.cached and len(calls) == 1
    args[change] = {"host": "two.github.io", "port": 80, "capability": "proxy.connect",
                    "intent": "upload"}[change]
    assert not (await classifier.classify_target(**args)).cached
    assert len(calls) == 2


def _approve(store, decision="allow_once"):
    item = store.request(task_id="task-a", fence_token=1, kind="network",
        capability="tool.egress", target={"domain": "docs.example.com", "port": 443},
        operation_id="op-a", expires_at=time.time() + 60)
    return store.resolve(item.approval_id, decision=decision, actor="human", reason="reviewed",
                         expected_fence_token=1)


def test_allow_once_never_creates_target_grant(tmp_path):
    store = SecurityApprovalStore("run-a", runs_dir=str(tmp_path))
    item = _approve(store)
    store.consume(item.approval_id, operation_id="op-a", expected_fence_token=1)
    assert not store.target_state(1)["targets"]
    with pytest.raises(ValueError, match="not_usable"):
        store.consume(item.approval_id, operation_id="op-b", expected_fence_token=1)


def test_human_revoke_survives_reload_and_invalidates_allow_run(tmp_path):
    store = SecurityApprovalStore("run-a", runs_dir=str(tmp_path))
    item = _approve(store, "allow_run")
    target = store.target_state(1)["targets"][0]
    result = store.decide_target(target["target_id"], decision="revoke", reason="changed",
                                actor="human", expected_version=target["version"], fence_token=1)
    restored = SecurityApprovalStore("run-a", runs_dir=str(tmp_path))
    assert restored.target_state(1)["targets"][0]["decision"] == "revoke"
    with pytest.raises(ValueError, match="revoked"):
        restored.consume(item.approval_id, operation_id="op-a", expected_fence_token=1)
    with pytest.raises(ValueError, match="version_conflict"):
        restored.decide_target(target["target_id"], decision="allow_run", reason="stale",
            actor="human", expected_version=target["version"], fence_token=1)
    assert result["version"] == target["version"] + 1


@pytest.mark.asyncio
async def test_revocation_beats_in_flight_model(monkeypatch):
    internal = FakeInternal()
    runtime = _runtime_with_run(internal)

    async def invoke(call):
        internal.target_decision = "revoke"
        internal.target_version = 1
        return EgressModelReply(status="completed", content="allow")

    monkeypatch.setattr(runtime, "_egress_model_invoker", lambda *a, **k: invoke)
    result = await _precheck(runtime, _profile())
    assert (result.decision, result.source) == ("ask", "human_revoked")


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline,override,expected", [
    ("allow", "manual", "ask"), ("allow", "auto", "allow"), ("auto", "manual", "ask"),
])
async def test_effective_mode_controls_real_gateway_precheck(monkeypatch, baseline, override, expected):
    runtime = _runtime_with_run(FakeInternal(override={"mode": override}))

    async def invoke(call):
        return EgressModelReply(status="completed", content="allow")

    monkeypatch.setattr(runtime, "_egress_model_invoker", lambda *a, **k: invoke)
    assert (await _precheck(runtime, _profile(baseline))).decision == expected


@pytest.mark.asyncio
async def test_auto_proxy_unknown_target_never_calls_model():
    runtime = _runtime_with_run(FakeInternal())
    result = await _precheck(runtime, _profile(), capability="proxy.connect")
    assert (result.decision, result.source) == ("ask", "capability_requires_human")
    assert not runtime.egress_classifiers


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected_calls", [("manual", 0), ("auto", 1), ("manual-approved", 1)])
async def test_gateway_execution_respects_narrowed_open_baseline(tmp_path, monkeypatch, mode, expected_calls):
    from types import SimpleNamespace

    from open_deep_research.sandbox import gateway
    from open_deep_research.sandbox.wire import GatewayToolRequestV1
    from open_deep_research.tools.base import ToolExecutionZone

    store = SecurityApprovalStore("run-1", runs_dir=str(tmp_path))

    class Authority(FakeInternal):
        async def post(self, path, request):
            assert request.service_nonce not in nonces, "internal request nonce replayed after approval"
            nonces.add(request.service_nonce)
            if path.endswith("/approvals/request"):
                return store.request(task_id=request.task_id, fence_token=request.fence_token,
                    kind=request.kind, capability=request.capability, target=request.target,
                    operation_id=request.operation_id, expires_at=request.expires_at).model_dump()
            if "/budgets/" in path:
                return {}
            return await super().post(path, request)

    nonces = set()
    runtime = _runtime_with_run(Authority(override={"mode": "manual" if mode == "manual-approved" else mode}))
    # Exercise the actual signed request shape, including one-use nonces.
    from open_deep_research.sandbox.internal_api import SandboxInternalClient
    from tests.test_egress_gateway_wiring import ROOT_KEY
    runtime.internal.signed = SandboxInternalClient("http://authority", ROOT_KEY).signed
    if mode == "manual-approved":
        async def approved(*args, **kwargs):
            return "allowed", SimpleNamespace(decision="allow_once")
        monkeypatch.setattr(runtime, "_request_network_approval", approved)
    tool = SimpleNamespace(name="fetch_url", execution_zone=ToolExecutionZone.GATEWAY,
        effect="read_only", egress_urls=lambda args: [args["url"]])

    async def assemble(*args):
        return [tool]

    async def public(*args):
        return None

    async def model(call):
        return EgressModelReply(status="completed", content="allow")

    emitted = []

    async def dispatch(call, *args, **kwargs):
        # The transport stub is behind the real, server-injected callback.
        from open_deep_research.sandbox.egress_context import authorize_url
        assert await authorize_url(call["args"]["url"], consume=True) == "allow"
        emitted.append(call)
        return SimpleNamespace(error=None, result=SimpleNamespace(output="fetched"))

    monkeypatch.setattr(gateway, "resolve_profile", lambda config: (None, "p", _profile("allow")))
    monkeypatch.setattr(gateway, "tool_policy_decision", lambda *a, **k: "allow")
    monkeypatch.setattr("open_deep_research.agentscope_runtime.sandbox_catalog.assembled_tools", assemble)
    monkeypatch.setattr("open_deep_research.tools.governance.execute_governed_tool_call_native", dispatch)
    monkeypatch.setattr("open_deep_research.security.network.validate_public_http_url", public)
    monkeypatch.setattr(runtime, "_egress_model_invoker", lambda *a, **k: model)
    request = GatewayToolRequestV1(run_id="run-1", task_id="task-1", role="researcher",
        stage="researching", logical_operation_id="fetch-1", tool_call_id="call-1",
        tool_name="fetch_url", arguments={"url": "https://docs.example/page"}, execution_zone="gateway")
    result = await runtime.invoke_tool(request, runtime.runs["run-1"])
    assert len(emitted) == expected_calls
    assert result.status == ("completed" if expected_calls else "approval_required")


@pytest.mark.asyncio
async def test_proxy_rechecks_mode_after_dns(monkeypatch):
    from types import SimpleNamespace

    from open_deep_research.sandbox import egress_proxy

    internal = FakeInternal(override={"mode": "manual"})
    runtime = _runtime_with_run(internal)
    monkeypatch.setattr(egress_proxy, "resolve_profile", lambda config: (None, "p", _profile("allow")))
    proxy = egress_proxy.GatewayEgressProxy(runtime)
    claims = SimpleNamespace(run_id="run-1", task_id="task-1", fence_token=1)
    assert not await proxy._connection_still_allowed(claims, "docs.example", 443, "connect-1", None)


def test_allow_once_concurrent_consumers_have_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = SecurityApprovalStore("run-a", runs_dir=str(tmp_path))
    approval = _approve(store)

    def consume(_):
        try:
            store.consume(approval.approval_id, operation_id="op-a", expected_fence_token=1)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(consume, range(2))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "100.64.0.1", "::1"])
async def test_http_connector_rejects_rebinding_before_socket_connect(monkeypatch, address):
    import aiohttp
    from aiohttp.resolver import ThreadedResolver

    from open_deep_research.security.network import PublicWebResolver

    async def rebound(*args):
        return [{"host": address}]

    async def forbidden_dial(*args, **kwargs):
        pytest.fail("No socket may be opened for the rebound private address")

    monkeypatch.setattr(ThreadedResolver, "resolve", rebound)
    connector = aiohttp.TCPConnector(resolver=PublicWebResolver())
    monkeypatch.setattr(connector, "_wrap_create_connection", forbidden_dial)
    async with aiohttp.ClientSession(connector=connector) as session:
        with pytest.raises(ValueError, match="private, local, or reserved"):
            await session.get("https://previously-public.example/page")


def test_classifier_ledger_failure_keeps_manual_state_available(tmp_path, monkeypatch):
    from tests.test_egress_mode_api import (
        _POLICY_AUTO,
        _bypass_client,
        _live_run,
        _write_policy,
    )

    client = _bypass_client(monkeypatch, tmp_path)
    _live_run("run-health", tmp_path, _write_policy(tmp_path, _POLICY_AUTO))

    def corrupt(*args):
        raise ValueError("corrupt classification ledger")

    monkeypatch.setattr(EgressClassificationStore, "load", corrupt)
    try:
        response = client.get("/runs/run-health/egress-state")
        assert response.status_code == 200
        assert response.json()["can_resolve"]
        assert response.json()["health"]["reason"] == "state_unavailable"
        assert "remaining_calls" not in response.json()["health"]
    finally:
        from tests.test_egress_mode_api import _clear_native_runs
        _clear_native_runs()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["legacy", "litellm"])
async def test_model_backends_preserve_budget_temperature_and_served_model(monkeypatch, backend):
    from types import SimpleNamespace

    from open_deep_research.sandbox.egress_classifier import EgressModelCall

    monkeypatch.setenv("MODEL_BACKEND", backend)
    runtime = _runtime_with_run(FakeInternal())
    requests = []

    async def invoke(request, context):
        requests.append(request)
        assert request.messages[0]["content"] == "Research request"
        return SimpleNamespace(status="completed", served_model="actual-model", structured=None,
                               message={"role": "assistant", "content": "allow"})

    monkeypatch.setattr(runtime, "invoke_model_operation_v2", invoke)
    call = EgressModelCall(messages=[{"role": "user", "content": "Research request"}],
        logical_operation_id="classify", max_output_tokens=48, temperature=0)
    result = await runtime._egress_model_invoker(runtime.runs["run-1"], run_id="run-1",
                                               task_id="task-1", stage="researching")(call)
    assert result.served_model == "actual-model"
    assert requests[0].max_output_tokens == 48 and requests[0].temperature == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["https://other.example/page", "https://docs.example:8443/page"])
async def test_redirect_reapproval_prevents_destination_request(monkeypatch, target):
    from types import SimpleNamespace

    from open_deep_research.web import pipeline
    from open_deep_research.agentscope_runtime.web_tools import _candidate
    def candidate(url):
        return _candidate("test", url, "Source", "", 1, "test")

    emitted = []

    class Response:
        status = 302
        headers = {"Location": target}
        connection = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            emitted.append(url)
            return Response()

    async def validate(*args, **kwargs):
        return SimpleNamespace()

    async def allowed(url, capability, consume):
        return "deny" if url == target else "allow"

    monkeypatch.setattr(pipeline.aiohttp, "ClientSession", lambda **kwargs: Session())
    monkeypatch.setattr(pipeline, "validate_public_http_url", validate)
    monkeypatch.setattr(pipeline, "validate_response_peer", lambda *args: None)
    token = egress_authorizer.set(allowed)
    try:
        raw = await pipeline.fetch_local(candidate("https://docs.example/start"),
                                          pipeline.WebPipelineSettings(respect_robots_txt=False))
    finally:
        egress_authorizer.reset(token)
    assert emitted == ["https://docs.example/start"]
    assert not raw.result.success


@pytest.mark.asyncio
async def test_external_extraction_does_not_inherit_readonly_allow(monkeypatch):
    from open_deep_research.agentscope_runtime import web_tools as pipeline

    checks = []
    def forbidden(_config):
        pytest.fail("external extraction must be authorized before constructing its client")

    async def allowed(url, capability, consume):
        checks.append((capability, consume))
        return "allow" if capability == "tool.egress" else "ask"

    token = egress_authorizer.set(allowed)
    try:
        result = await pipeline._tavily_extract("https://docs.example/page", forbidden)
    finally:
        egress_authorizer.reset(token)
    assert result is None
    assert checks == [("external.extract", True)]


@pytest.mark.asyncio
async def test_classifier_budget_survives_restart():
    internal = FakeInternal()
    runtime = _runtime_with_run(internal)
    runtime.runs["run-1"].config["configurable"]["egress_classifier_max_calls_per_run"] = 1
    classifier = await runtime._egress_classifier("run-1", runtime.runs["run-1"])

    async def invoke(call):
        return EgressModelReply(status="completed", content="allow")

    await classifier.classify_target(host="one.example", port=443, tool_name="fetch_url",
                                     capability="tool.egress", invoker=invoke)
    runtime.egress_classifiers.clear()
    revived = await runtime._egress_classifier("run-1", runtime.runs["run-1"])
    result = await revived.classify_target(host="two.example", port=443, tool_name="fetch_url",
                                          capability="tool.egress", invoker=invoke)
    assert result.verdict == "ask" and result.detail == "classifier_budget_exhausted"


def test_ledger_update_and_legacy_cache_migration(tmp_path):
    store = EgressClassificationStore("run-a", runs_dir=str(tmp_path))
    entry = EgressClassificationEntry(fingerprint=classification_fingerprint("a.example"),
        registered_domain="a.example", host="a.example", port=443, verdict="ask",
        source="stage2", classified_at=1)
    assert store.record(entry.to_payload()) == "recorded"
    updated = {**entry.to_payload(), "verdict": "deny", "classified_at": 2}
    assert store.record(updated) == "recorded"
    assert store.record(entry.to_payload()) == "duplicate"
    assert store.load()[entry.fingerprint]["verdict"] == "deny"


@pytest.mark.asyncio
async def test_nested_candidate_approval_is_not_bypassed_by_gateway_metadata():
    from open_deep_research.agentscope_runtime.web_tools import (
        _approve_candidate_batch,
        _candidate,
    )
    candidate = _candidate("test", "https://blocked.example/page", "", "", 1, "test")
    calls = []

    async def deny(url, capability, consume):
        calls.append(url)
        return "deny"

    token = egress_authorizer.set(deny)
    try:
        result = await _approve_candidate_batch([candidate], 1,
            {"configurable": {"sandbox_enabled": False}, "metadata": {"sandbox_gateway_physical": True}}, "run")
    finally:
        egress_authorizer.reset(token)
    assert result.denied_domains == ["blocked.example"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_denied_fetch_emits_no_http_request(monkeypatch):
    from open_deep_research.agentscope_runtime.web_tools import _candidate
    from open_deep_research.web.pipeline import WebPipelineSettings, fetch_local

    def fail_get(*args, **kwargs):
        pytest.fail("HTTP must not start before authorization, including robots.txt")

    monkeypatch.setattr("aiohttp.ClientSession.get", fail_get)

    async def deny(url, capability, consume):
        return "deny"

    token = egress_authorizer.set(deny)
    try:
        result = await fetch_local(_candidate("test", "https://blocked.example/", "", "", 1, "test"),
                                   WebPipelineSettings())
    finally:
        egress_authorizer.reset(token)
    assert not result.result.success
    assert result.result.failure_class == "approval_required"


def test_target_management_api_and_stale_version(tmp_path, monkeypatch):
    from tests.test_egress_mode_api import (
        _POLICY_AUTO,
        _bypass_client,
        _live_run,
        _write_policy,
    )

    policy = _write_policy(tmp_path, _POLICY_AUTO)
    client = _bypass_client(monkeypatch, tmp_path)
    _live_run("run-target", tmp_path, policy)
    store = SecurityApprovalStore("run-target", runs_dir=str(tmp_path))
    target = store.observe_target("tool.egress", {"domain": "docs.example.com", "port": 443}, 1)
    url = f"/runs/run-target/egress-targets/{target['target_id']}/decision"
    try:
        state = client.get("/runs/run-target/egress-state")
        assert state.status_code == 200
        assert state.json()["can_resolve"]
        assert "open" not in state.json()["allowed_modes"]
        response = client.post(url, json={"decision": "allow_run", "expected_version": 0})
        assert response.status_code == 200
        assert client.post(url, json={"decision": "revoke", "expected_version": 0}).status_code == 409
        assert client.post(url, json={"decision": "revoke", "expected_version": 1}).status_code == 200
        assert client.get("/runs/run-target/egress-state").json()["targets"][0]["decision"] == "revoke"
    finally:
        from tests.test_egress_mode_api import _clear_native_runs
        _clear_native_runs()


@pytest.mark.asyncio
async def test_proxy_once_allows_one_connection_only(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from open_deep_research.sandbox import egress_proxy

    store = SecurityApprovalStore("run-1", runs_dir=str(tmp_path))

    class Authority(FakeInternal):
        async def post(self, path, request):
            if path.endswith("/target/check"):
                target = store.observe_target(request.capability, request.target, request.fence_token)
                _, approvals = store.list()
                return {**target, "approvals": [a.model_dump() for a in approvals
                                                if a.target_fingerprint == target["target_id"]]}
            if path.endswith("/approvals/request"):
                return store.request(task_id=request.task_id, fence_token=request.fence_token,
                    kind=request.kind, capability=request.capability, target=request.target,
                    operation_id=request.operation_id, expires_at=request.expires_at).model_dump()
            if path.endswith("/approvals/consume"):
                return store.consume(request.approval_id, operation_id=request.operation_id,
                                     expected_fence_token=request.fence_token).model_dump()
            return await super().post(path, request)

    runtime = _runtime_with_run(Authority())
    proxy = egress_proxy.GatewayEgressProxy(runtime)
    monkeypatch.setattr(egress_proxy, "resolve_profile", lambda config: (None, "p", _profile()))
    monkeypatch.setattr(egress_proxy, "decode_task_token", lambda *a: SimpleNamespace(
        run_id="run-1", task_id="task-1", fence_token=1, jti="token", expires_at=time.time() + 60))

    async def public(*args):
        return "8.8.8.8"

    monkeypatch.setattr(proxy, "_resolve", public)
    args = dict(task_token="test", timestamp=time.time(), host="docs.example.com", port=443,
                method="CONNECT", operation_id="connect-1")
    allowed, approval_id, _ = await proxy._authorize(**args, nonce="first-connection-nonce")
    assert not allowed and approval_id
    store.resolve(approval_id, decision="allow_once", actor="human", reason="", expected_fence_token=1)
    assert (await proxy._authorize(**args, nonce="second-connection-nonce"))[0]
    third = await proxy._authorize(**args, nonce="third-connection-nonce")
    assert not third[0] and third[1] != approval_id
    assert not runtime.egress_classifiers
