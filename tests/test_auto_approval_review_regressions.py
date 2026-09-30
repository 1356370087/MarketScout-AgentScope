"""Regression coverage for the four Auto approval review findings."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from open_deep_research.api.security_routes import _runtime_egress_override
from open_deep_research.sandbox.approvals import SecurityApprovalStore
from open_deep_research.sandbox.egress_classifier import (
    EgressClassificationEntry,
    EgressClassifier,
    EgressClassifierLimits,
    EgressModelReply,
    InMemoryEgressLedger,
    classification_fingerprint,
)
from open_deep_research.sandbox.egress_ledger_store import RunEgressModeStore
from open_deep_research.sandbox.policy import egress_target_from_url
from open_deep_research.sandbox.schema import NetworkPolicy, domain_matches
from open_deep_research.sandbox.wire import GatewayToolRequestV1
from open_deep_research.tools.base import ToolExecutionZone
from tests.test_egress_gateway_wiring import (
    FakeInternal,
    _precheck,
    _profile,
    _runtime_with_run,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("verdict", ["deny", "ask", "allow"])
async def test_authorization_precedes_dns(monkeypatch, nested, verdict):
    events = []

    class Internal(FakeInternal):
        async def post(self, path, request):
            if "/budgets/" in path:
                return {}
            return await super().post(path, request)

    runtime = _runtime_with_run(Internal())
    runtime.runs["run-1"].config["configurable"]["egress_classifier_stages"] = "fast"
    url = "https://synthetic-confidential-marker.receiver.example/document"
    tool = SimpleNamespace(
        name="web_research" if nested else "fetch_url", effect="read_only",
        execution_zone=ToolExecutionZone.GATEWAY,
        egress_urls=lambda args: [] if nested else [url],
    )

    async def assemble(*args):
        return [tool]

    async def model(call):
        events.append("classification")
        return EgressModelReply(status="completed", content=verdict)

    def dns(*args, **kwargs):
        events.append("dns")
        # Even an approved domain must not bypass private-address checks.
        return [(2, 1, 6, "", ("127.0.0.1", 443))]

    async def pending(*args, **kwargs):
        return "pending", SimpleNamespace(approval_id="review-approval")

    async def execute(call, *args, **kwargs):
        from open_deep_research.sandbox.egress_context import authorize_url

        decision = await authorize_url(url, consume=True)
        assert decision == ("ask" if verdict == "ask" else "deny")
        return SimpleNamespace(error=None, result=SimpleNamespace(output=decision))

    monkeypatch.setattr("open_deep_research.agentscope_runtime.sandbox_catalog.assembled_tools", assemble)
    monkeypatch.setattr("open_deep_research.sandbox.gateway.resolve_profile", lambda c: (None, "p", _profile()))
    monkeypatch.setattr("open_deep_research.sandbox.gateway.tool_policy_decision", lambda *a, **k: "allow")
    monkeypatch.setattr("open_deep_research.tools.governance.execute_governed_tool_call_native", execute)
    monkeypatch.setattr("open_deep_research.security.network.socket.getaddrinfo", dns)
    monkeypatch.setattr(runtime, "_egress_model_invoker", lambda *a, **k: model)
    monkeypatch.setattr(runtime, "_request_network_approval", pending)
    request = GatewayToolRequestV1(
        run_id="run-1", task_id="task-1", role="researcher", stage="researching",
        logical_operation_id="review-dns", tool_call_id="review-dns",
        tool_name=tool.name, execution_zone="gateway", arguments={"url": url},
    )
    result = await runtime._invoke_tool(request, runtime.runs["run-1"])
    assert events == (["classification", "dns"] if verdict == "allow" else ["classification"])
    if not nested and verdict != "allow":
        assert result.status == ("approval_required" if verdict == "ask" else "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["block_run", "revoke", "manual"])
async def test_recovery_preserves_restrictions_over_cached_allow(tmp_path, restriction):
    store = SecurityApprovalStore("run-1", runs_dir=str(tmp_path))
    modes = RunEgressModeStore("run-1", runs_dir=str(tmp_path))
    target = {"domain": "docs.example.com", "port": 443}
    observed = store.observe_target("tool.egress", target, 1)
    if restriction == "manual":
        modes.set(mode="manual", actor="user", fence_token=1)
    else:
        store.decide_target(observed["target_id"], decision=restriction, reason="keep private",
                            actor="user", expected_version=0, fence_token=1)
    entry = EgressClassificationEntry(
        fingerprint=classification_fingerprint(target["domain"]), registered_domain="example.com",
        host=target["domain"], port=443, verdict="allow", source="stage1",
    )

    class Internal(FakeInternal):
        async def post(self, path, request):
            if path.endswith("/egress/mode/get"):
                mode = _runtime_egress_override("run-1", str(tmp_path), fence_token=request.fence_token)
                return {"override": {"mode": mode} if mode else None}
            if path.endswith("/egress/target/check"):
                return store.check_target(request.capability, request.target, request.fence_token)
            if path.endswith("/egress/classifications/load"):
                return {"entries": {entry.fingerprint: entry.to_payload()}, "health": {}}
            return await super().post(path, request)

    runtime = _runtime_with_run(Internal())
    runtime.runs["run-1"].fence_token = 2
    # Recovery must also display restrictions before another tool accesses them.
    state = store.target_state(2)["targets"][0]
    assert state["decision"] == (None if restriction == "manual" else restriction)
    result = await runtime._egress_precheck(
        run_id="run-1", task_id="task-1", fence_token=2, stage="researching",
        host=target["domain"], port=443, tool_name="fetch_url", capability="tool.egress",
        operation_id="resumed", profile=_profile(),
    )
    assert result.decision == ("deny" if restriction == "block_run" else "ask")
    assert not runtime.egress_classifiers
    with pytest.raises(ValueError, match="stale_fence"):
        store.check_target("tool.egress", target, 1)
    with pytest.raises(ValueError, match="version_conflict"):
        store.decide_target(observed["target_id"], decision="allow_run", reason="stale UI",
                            actor="user", expected_version=0, fence_token=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_classification_never_publishes_allow(cancel):
    responding = asyncio.Event()
    classifier = EgressClassifier(
        EgressClassifierLimits(timeout_seconds=0.05, max_calls_per_run=1, max_consecutive_failures=1),
        ledger=InMemoryEgressLedger(),
    )

    async def allow(call):
        responding.set()
        await asyncio.sleep(10)
        return EgressModelReply(status="completed", content="allow")

    args = {"host": "docs.example.com", "port": 443, "capability": "tool.egress", "tool_name": "fetch_url", "invoker": allow}
    task = asyncio.create_task(classifier.classify_target(**args))
    await responding.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).detail == "timeout"
    assert classifier.lookup(args["host"]) is None
    assert (await classifier.classify_target(**args)).verdict == "ask"
    assert await classifier.ledger.load() == {}


@pytest.mark.asyncio
async def test_slow_cache_ack_does_not_report_timeout_then_reuse_allow():
    class SlowAckLedger(InMemoryEgressLedger):
        async def record(self, entry):
            await super().record(entry)
            await asyncio.sleep(0.05)

    ledger = SlowAckLedger()
    classifier = EgressClassifier(EgressClassifierLimits(timeout_seconds=0.02), ledger=ledger)

    async def allow(call):
        return EgressModelReply(status="completed", content="allow")

    args = {"host": "docs.example.com", "port": 443, "tool_name": "fetch_url", "capability": "tool.egress", "invoker": allow}
    first = await classifier.classify_target(**args)
    assert first.verdict == "allow" and not first.cached
    assert (await classifier.classify_target(**args)).cached
    revived = EgressClassifier(EgressClassifierLimits(), ledger=ledger)
    await revived.warm()
    assert (await revived.classify_target(**args)).verdict == first.verdict


@pytest.mark.asyncio
async def test_health_write_failure_never_publishes_allow():
    class FailingHealthLedger(InMemoryEgressLedger):
        async def save_state(self, state):
            if state["revision"] == 2:
                raise OSError("health unavailable")

    classifier = EgressClassifier(EgressClassifierLimits(), ledger=FailingHealthLedger())

    async def allow(call):
        return EgressModelReply(status="completed", content="allow")

    with pytest.raises(OSError):
        await classifier.classify_target(host="docs.example.com", port=443,
            capability="tool.egress", tool_name="fetch_url", invoker=allow)
    assert classifier.lookup("docs.example.com") is None
    assert await classifier.ledger.load() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern,host", [
    ("BÜCHER.example.", "bücher.example"),
    ("*.bücher.example", "docs.bücher.example"),
    ("**.bücher.example", "deep.docs.bücher.example"),
])
async def test_idna_blacklist_never_reaches_classifier(pattern, host):
    profile = _profile()
    profile.network = NetworkPolicy(unknown_target="auto", deny_domains=[pattern])
    normalized, _ = egress_target_from_url(f"https://{host}/")
    runtime = _runtime_with_run(FakeInternal())
    assert (await _precheck(runtime, profile, host=normalized)).decision == "deny"
    assert not runtime.egress_classifiers
    assert domain_matches(pattern, normalized)
    assert not domain_matches(pattern, "docs.bücher.example.attacker.example")


def test_idna_single_label_wildcard_does_not_widen():
    assert not domain_matches("*.bücher.example", "bücher.example")
    assert not domain_matches("*.bücher.example", "deep.docs.bücher.example")


@pytest.mark.asyncio
async def test_proxy_denial_does_not_resolve_target(monkeypatch):
    from open_deep_research.sandbox import egress_proxy

    runtime = _runtime_with_run(FakeInternal())
    claims = SimpleNamespace(run_id="run-1", task_id="task-1", fence_token=1,
                             jti="review-proxy", expires_at=time.time() + 60)
    monkeypatch.setattr(egress_proxy, "decode_task_token", lambda *a: claims)
    monkeypatch.setattr(egress_proxy, "resolve_profile", lambda c: (None, "p", _profile("deny")))

    async def forbidden_dns(*args):
        pytest.fail("A denied proxy target must not be resolved")

    proxy = egress_proxy.GatewayEgressProxy(runtime)
    monkeypatch.setattr(proxy, "_resolve", forbidden_dns)
    allowed, _, _ = await proxy._authorize(task_token="fixture", timestamp=time.time(),
        nonce="review-proxy-nonce-0001", host="private-data.receiver.example", port=443,
        method="CONNECT", operation_id="proxy-review")
    assert not allowed


@pytest.mark.asyncio
async def test_robots_authorization_precedes_dns(monkeypatch):
    from open_deep_research.sandbox.egress_context import egress_authorizer
    from open_deep_research.web import pipeline

    async def deny(*args, **kwargs):
        return "deny"

    async def forbidden_dns(*args):
        pytest.fail("Denied robots URLs must not reach DNS")

    monkeypatch.setattr(pipeline, "validate_public_http_url", forbidden_dns)
    token = egress_authorizer.set(deny)
    try:
        with pytest.raises(PermissionError):
            await pipeline._robots_allowed(None, "https://denied-robots.example/",
                SimpleNamespace(respect_robots_txt=True, cache_namespace="review-robots"))
    finally:
        egress_authorizer.reset(token)


def test_recovery_does_not_promote_old_positive_approvals(tmp_path):
    store = SecurityApprovalStore("run-1", runs_dir=str(tmp_path))
    target = {"domain": "docs.example.com", "port": 443}
    approval = store.request(task_id="task-1", fence_token=1, kind="network",
        capability="tool.egress", target=target, operation_id="old-operation",
        expires_at=time.time() + 60)
    store.resolve(approval.approval_id, decision="allow_run", actor="user",
                  reason="old permission", expected_fence_token=1)
    state = store.check_target("tool.egress", target, 2)
    assert state["decision"] is None and state["approvals"] == []
    with pytest.raises(ValueError, match="stale_fence"):
        store.consume(approval.approval_id, operation_id="old-operation", expected_fence_token=2)
