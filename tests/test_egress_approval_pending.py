"""Gateway approval_required fast-path and pending-turn signal tests."""

import base64
import time
from types import SimpleNamespace

import httpx
import pytest

from open_deep_research.configuration import Configuration
from open_deep_research.sandbox.approvals import SecurityApproval, SecurityApprovalStore
from open_deep_research.sandbox.gateway import (
    GatewayRunContext,
    GatewayRuntime,
)
from open_deep_research.sandbox.gateway_tool import GatewayToolProxy
from open_deep_research.sandbox.wire import GatewayToolOutcomeV1
from open_deep_research.tools.governance import (
    ApprovalPendingError,
    ToolErrorType,
    classify_retryable_error,
)

ROOT_KEY = base64.b64encode(b"k" * 32).decode()


def _config(**overrides):
    values = {
        "sandbox_enabled": True,
        "enable_async_research": True,
        "sandbox_root_signing_key": ROOT_KEY,
        "sandbox_policy_path": "config/sandbox-policy.toml",
        "runs_dir": ".runs-test-approval-pending",
    }
    values.update(overrides)
    return Configuration(**values)


class FakeInternal:
    """Path-routed fake covering the approval endpoints."""

    def __init__(self, *, wait_responses=None, created=None, consume_status=None):
        self.wait_responses = list(wait_responses or [[]])
        self.created = list(created or [])
        self.consume_calls = 0
        self.consume_status = consume_status
        self.create_calls = []
        self.wait_calls = []

    def signed(self, _kind, **payload):
        return SimpleNamespace(**payload)

    async def post(self, path, request):
        if path == "/internal/sandbox/egress/target/check":
            return {"version": 0, "decision": None}
        if path == "/internal/sandbox/approvals/wait":
            self.wait_calls.append(request)
            if len(self.wait_responses) > 1:
                payload = self.wait_responses.pop(0)
            else:
                payload = self.wait_responses[0] if self.wait_responses else []
            return {"version": 1, "approvals": payload}
        if path == "/internal/sandbox/approvals/request":
            self.create_calls.append(request)
            return self.created.pop(0)
        if path == "/internal/sandbox/approvals/consume":
            self.consume_calls += 1
            if self.consume_status is not None:
                response = httpx.Response(
                    status_code=self.consume_status,
                    request=httpx.Request("POST", path),
                )
                raise httpx.HTTPStatusError(
                    "conflict", request=response.request, response=response
                )
            return {"status": "consumed"}
        raise AssertionError(f"unexpected internal path {path}")


def _runtime(internal, config=None):
    configurable = config or _config()
    runtime = GatewayRuntime(configurable)
    runtime.internal = internal
    runtime.runs["run-1"] = GatewayRunContext(
        config={
            "configurable": configurable.model_dump(mode="json"),
            "metadata": {},
        },
        fence_token=1,
        expires_at=time.time() + 60,
    )
    return runtime


def _request(
    run_id="run-1",
    task_id="task-1",
    operation_id="op-1",
    *,
    arguments=None,
):
    return SimpleNamespace(
        run_id=run_id,
        task_id=task_id,
        role="researcher",
        stage="researching",
        tool_name="fetch_url",
        arguments=arguments or {"url": "https://docs.example.com/source"},
        logical_operation_id=operation_id,
    )


def _network_operation_id(request, *, host="docs.example.com", port=443):
    operation_key = GatewayRuntime._network_approval_operation_key(  # noqa: SLF001
        request,
        host=host,
        port=port,
    )
    return GatewayRuntime._network_approval_operation_id(  # noqa: SLF001
        request,
        operation_key=operation_key,
    )


def _approval(
    *,
    decision=None,
    status="pending",
    operation_id="op-1",
    domain="docs.example.com",
    port=443,
    fence_token=1,
):
    target = {"domain": domain, "port": port}
    return SecurityApproval(
        approval_id=f"apr-{domain}",
        run_id="run-1",
        task_id="task-1",
        fence_token=fence_token,
        kind="network",
        capability="tool.egress",
        target=target,
        target_fingerprint=SecurityApprovalStore.fingerprint(
            "network", "tool.egress", target
        ),
        status=status,
        decision=decision,
        expires_at=time.time() + 60,
        operation_id=operation_id,
    )


def _as_payload(approval):
    return approval.model_dump(mode="json")


class TestRequestNetworkApproval:
    @pytest.mark.asyncio
    async def test_first_call_creates_and_returns_pending(self):
        created = _approval(status="pending", domain="docs.example.com")
        internal = FakeInternal(created=[created.model_dump(mode="json")])
        runtime = _runtime(internal)
        state, approval = await runtime._request_network_approval(
            _request(),
            runtime.runs["run-1"],
            host="docs.example.com",
            port=443,
            expires_at=time.time() + 60,
        )
        assert state == "pending"
        assert approval.status == "pending"
        assert len(internal.create_calls) == 1

    @pytest.mark.asyncio
    async def test_retry_attaches_to_pending_and_blocks_until_allow(self):
        original = _request(operation_id="op-original")
        retry = _request(operation_id="op-retry")
        approval_operation_id = _network_operation_id(original)
        pending = _approval(operation_id=approval_operation_id)
        resolved = _approval(
            decision="allow_once",
            status="resolved",
            operation_id=approval_operation_id,
        )
        internal = FakeInternal(
            wait_responses=[
                [pending.model_dump(mode="json")],
                [resolved.model_dump(mode="json")],
            ]
        )
        runtime = _runtime(internal)
        state, approval = await runtime._request_network_approval(
            retry,
            runtime.runs["run-1"],
            host="docs.example.com",
            port=443,
            expires_at=time.time() + 60,
        )
        assert state == "allowed"
        assert approval.decision == "allow_once"
        # A brand-new approval was never created for the retry.
        assert internal.create_calls == []

    @pytest.mark.asyncio
    async def test_retry_attach_window_expires_back_to_pending(self):
        original = _request(operation_id="op-original")
        retry = _request(operation_id="op-retry")
        pending = _approval(operation_id=_network_operation_id(original))
        internal = FakeInternal(
            wait_responses=[[pending.model_dump(mode="json")]]
        )
        runtime = _runtime(
            internal,
            _config(sandbox_egress_pending_wait_seconds=0.01),
        )
        state, approval = await runtime._request_network_approval(
            retry,
            runtime.runs["run-1"],
            host="docs.example.com",
            port=443,
            expires_at=time.time() + 0.05,
        )
        assert state == "pending"
        assert approval.status == "pending"
        assert internal.wait_calls[1:]
        assert max(call.timeout_seconds for call in internal.wait_calls[1:]) <= 0.01

    @pytest.mark.asyncio
    async def test_same_domain_different_operation_does_not_attach(self):
        original = _request(
            operation_id="op-original",
            arguments={"url": "https://docs.example.com/source-a"},
        )
        unrelated = _request(
            operation_id="op-unrelated",
            arguments={"url": "https://docs.example.com/source-b"},
        )
        pending = _approval(operation_id=_network_operation_id(original))
        created = _approval(
            operation_id=_network_operation_id(unrelated),
        )
        internal = FakeInternal(
            wait_responses=[[pending.model_dump(mode="json")]],
            created=[created.model_dump(mode="json")],
        )
        runtime = _runtime(internal)

        state, approval = await runtime._request_network_approval(
            unrelated,
            runtime.runs["run-1"],
            host="docs.example.com",
            port=443,
            expires_at=time.time() + 60,
        )

        assert state == "pending"
        assert approval.approval_id == created.approval_id
        assert len(internal.create_calls) == 1

    @pytest.mark.asyncio
    async def test_allow_run_is_reusable_across_operations(self):
        reusable = _approval(
            decision="allow_run",
            status="resolved",
            operation_id="op-original",
        )
        internal = FakeInternal(
            wait_responses=[[reusable.model_dump(mode="json")]]
        )
        runtime = _runtime(internal)
        state, _approval_result = await runtime._request_network_approval(
            _request(operation_id="op-other"),
            runtime.runs["run-1"],
            host="docs.example.com",
            port=443,
            expires_at=time.time() + 60,
        )
        assert state == "allowed"
        assert internal.create_calls == []

    @pytest.mark.asyncio
    async def test_denied_decision_maps_to_denied(self):
        denied = _approval(decision="deny", status="resolved")
        internal = FakeInternal(
            wait_responses=[[]],
            created=[denied.model_dump(mode="json")],
        )
        runtime = _runtime(internal)
        state, _approval_result = await runtime._request_network_approval(
            _request(),
            runtime.runs["run-1"],
            host="docs.example.com",
            port=443,
            expires_at=time.time() + 60,
        )
        assert state == "denied"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["resolved", "expired"])
    async def test_retry_reuses_terminal_deny_without_new_approval(self, status):
        original = _request(operation_id="op-original")
        retry = _request(operation_id="op-retry")
        denied = _approval(
            decision="deny",
            status=status,
            operation_id=_network_operation_id(original),
        )
        internal = FakeInternal(
            wait_responses=[[denied.model_dump(mode="json")]],
        )
        runtime = _runtime(internal)

        state, approval = await runtime._request_network_approval(
            retry,
            runtime.runs["run-1"],
            host="docs.example.com",
            port=443,
            expires_at=time.time() + 60,
        )

        assert state == "denied"
        assert approval.approval_id == denied.approval_id
        assert internal.create_calls == []
        assert internal.consume_calls == 0


class TestPendingSignalMapping:
    @pytest.mark.asyncio
    async def test_allow_once_consume_conflict_is_not_authorized(self):
        """A failed atomic consume must never be treated as authorization."""
        original = _request(operation_id="op-creator")
        retry = _request(operation_id="op-attacher")
        operation_id = _network_operation_id(original)
        pending = _approval(operation_id=operation_id)
        resolved = _approval(
            decision="allow_once", status="resolved", operation_id=operation_id
        )
        internal = FakeInternal(
            wait_responses=[
                [pending.model_dump(mode="json")],
                [resolved.model_dump(mode="json")],
            ],
            consume_status=409,
        )
        runtime = _runtime(internal)
        with pytest.raises(httpx.HTTPStatusError):
            await runtime._request_network_approval(
                retry,
                runtime.runs["run-1"],
                host="docs.example.com",
                port=443,
                expires_at=time.time() + 60,
            )
        assert internal.consume_calls == 1

    @pytest.mark.asyncio
    async def test_non_conflict_consume_failure_propagates(self):
        original = _request(operation_id="op-creator")
        retry = _request(operation_id="op-attacher")
        operation_id = _network_operation_id(original)
        pending = _approval(operation_id=operation_id)
        resolved = _approval(
            decision="allow_once", status="resolved", operation_id=operation_id
        )
        internal = FakeInternal(
            wait_responses=[
                [pending.model_dump(mode="json")],
                [resolved.model_dump(mode="json")],
            ],
            consume_status=500,
        )
        runtime = _runtime(internal)
        with pytest.raises(httpx.HTTPStatusError):
            await runtime._request_network_approval(
                retry,
                runtime.runs["run-1"],
                host="docs.example.com",
                port=443,
                expires_at=time.time() + 60,
            )

    def test_approval_pending_classifies_non_retryable(self):
        error_type, retryable = classify_retryable_error(
            ApprovalPendingError("waiting", domain="www.gov.cn")
        )
        assert error_type is ToolErrorType.egress_domain_pending
        assert retryable is False

    @pytest.mark.asyncio
    async def test_proxy_maps_approval_required_outcome(self, monkeypatch):
        outcome = GatewayToolOutcomeV1(
            logical_operation_id="op-1",
            tool_call_id="call-1",
            status="approval_required",
            approval_id="apr-1",
            error={
                "error_type": "egress_domain_pending",
                "message": "Domain 'www.gov.cn' awaits approval.",
                "domain": "www.gov.cn",
            },
        )

        async def fake_call(_path, _tool, _input, _context, *, gateway_url=None, task_token=None):
            assert gateway_url is None and task_token is None
            return outcome

        monkeypatch.setattr(
            "open_deep_research.sandbox.gateway_tool._call_gateway", fake_call
        )
        delegate = SimpleNamespace(name="fetch_url")
        proxy = GatewayToolProxy(delegate=delegate)
        with pytest.raises(ApprovalPendingError) as exc_info:
            await proxy.call(
                input=SimpleNamespace(),
                context=SimpleNamespace(
                    config={"metadata": {}},
                    role="researcher",
                    tool_call_id="call-1",
                ),
            )
        assert exc_info.value.domain == "www.gov.cn"
        assert exc_info.value.approval_id == "apr-1"
