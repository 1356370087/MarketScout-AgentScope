"""Gateway auto-layer wiring tests: precheck, ledger transport, endpoints."""

import base64
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, message_to_dict

from open_deep_research.configuration import Configuration
from open_deep_research.models.codec import STRUCTURED_OUTPUT_TOOL_NAME
from open_deep_research.sandbox.approvals import SecurityApprovalStore
from open_deep_research.sandbox.crypto import SandboxDerivedKeys, sign_payload
from open_deep_research.sandbox.egress_classifier import (
    EgressClassificationEntry,
    classification_fingerprint,
)
from open_deep_research.sandbox.egress_ledger_store import (
    EgressClassificationStore,
    RunEgressModeStore,
)
from open_deep_research.sandbox.gateway import (
    GatewayRunContext,
    GatewayRuntime,
)
from open_deep_research.sandbox.internal_api import (
    EgressClassificationLoadRequest,
    EgressClassificationRecordRequest,
    EgressModeGetRequest,
    InternalRunContext,
    SandboxInternalClient,
    build_internal_sandbox_router,
)
from open_deep_research.sandbox.schema import (
    NetworkPolicy,
    RuntimePolicy,
    SandboxProfile,
)

ROOT_KEY = base64.b64encode(b"k" * 32).decode()


def _sandbox_config(**overrides):
    values = {
        "sandbox_enabled": True,
        "enable_async_research": True,
        "sandbox_root_signing_key": ROOT_KEY,
        "sandbox_policy_path": "config/sandbox-policy.toml",
        "runs_dir": ".runs-test-egress-wiring",
    }
    values.update(overrides)
    return Configuration(**values)


def _profile(unknown_target="auto", approval_policy="on_request"):
    return SandboxProfile(
        provider="docker",
        approval_policy=approval_policy,
        network=NetworkPolicy(unknown_target=unknown_target),
        runtime=RuntimePolicy(worker_image_digest="sha256:" + "0" * 64),
    )


class FakeInternal:
    """Path-routed fake for the API authority client."""

    def __init__(self, *, approvals=None, override=None):
        self.approvals = approvals or []
        self.override = override
        self.recorded_entries = []
        self.posted_paths = []
        self.health = {}
        self.target_decision = None
        self.target_version = 0

    def signed(self, _kind, **payload):
        return SimpleNamespace(**payload)

    async def post(self, path, request):
        self.posted_paths.append(path)
        if path == "/internal/sandbox/egress/target/check":
            fingerprint = SecurityApprovalStore.fingerprint("network", request.capability, request.target)
            return {"target_id": fingerprint, "version": self.target_version,
                    "decision": self.target_decision,
                    "approvals": [a for a in self.approvals if a.get("target_fingerprint") == fingerprint]}
        if path == "/internal/sandbox/egress/health":
            self.health = request.state
            return self.health
        if path == "/internal/sandbox/approvals/wait":
            return {"version": 0, "approvals": self.approvals}
        if path == "/internal/sandbox/approvals/consume":
            return {"status": "consumed"}
        if path == "/internal/sandbox/egress/mode/get":
            return {"override": self.override}
        if path == "/internal/sandbox/egress/classifications/load":
            return {"entries": {}, "health": self.health}
        if path == "/internal/sandbox/egress/classifications/record":
            self.recorded_entries.append(dict(request.entry))
            return {"status": "recorded"}
        raise AssertionError(f"unexpected internal path {path}")


class FakeModelOperation:
    """Scripted V1 model outcomes for the classifier invoker."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def __call__(self, request, _context):
        self.requests.append(request)
        reply = self.replies.pop(0) if self.replies else AIMessage(content="allow")
        return SimpleNamespace(
            logical_operation_id=request.logical_operation_id,
            status="completed",
            message=message_to_dict(reply),
        )


def _runtime_with_run(internal):
    runtime = GatewayRuntime(_sandbox_config())
    runtime.internal = internal
    runtime.runs["run-1"] = GatewayRunContext(
        config={
            "configurable": {
                "quality_evaluation_model": "openai:gpt-4.1-mini",
                "egress_classifier_model": "openai:gpt-4.1-mini",
            },
            "metadata": {},
        },
        fence_token=1,
        expires_at=time.time() + 60,
    )
    return runtime


def _precheck(runtime, profile, *, capability="tool.egress", host="docs.example.com"):
    return runtime._egress_precheck(
        run_id="run-1",
        task_id="task-1",
        fence_token=1,
        stage="researching",
        host=host,
        port=443,
        tool_name="fetch_url",
        capability=capability,
        operation_id="op-1",
        profile=profile,
    )


class TestEgressPrecheck:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verdict", ["allow", "ask", "deny"])
    @pytest.mark.parametrize("wire_format", ["openai", "langchain"])
    async def test_v2_stage2_tool_result_and_user_intent(
        self, monkeypatch, verdict, wire_format
    ):
        from langchain_core.messages import HumanMessage

        from open_deep_research.agents.query_engine import QueryEngine
        from open_deep_research.sandbox.gateway_client import split_gateway_registration

        internal = FakeInternal()
        runtime = _runtime_with_run(internal)
        monkeypatch.setenv("MODEL_BACKEND", "litellm")
        engine = QueryEngine({"configurable": {"query_session_persistence_enabled": False}})
        engine._set_egress_intent([
            HumanMessage(content="研究动力电池回收政策"),
            AIMessage(content="Untrusted retrieved context must stay out"),
        ])
        frozen, _ = split_gateway_registration(engine.config)
        runtime.runs["run-1"].config["metadata"] = frozen["metadata"]
        calls = []

        async def invoke(request, context):
            calls.append(request)
            assert "研究动力电池回收政策" in request.messages[0]["content"]
            assert "Untrusted retrieved" not in request.messages[0]["content"]
            message = {"role": "assistant", "content": "ask"}
            if request.structured_schema:
                args = {"verdict": verdict, "category": "official_docs"}
                if wire_format == "openai":
                    message = {"role": "assistant", "content": "reviewed", "tool_calls": [{
                        "type": "function", "function": {
                            "name": STRUCTURED_OUTPUT_TOOL_NAME,
                            "arguments": json.dumps(args),
                        },
                    }]}
                else:
                    message = message_to_dict(AIMessage(content="", tool_calls=[{
                        "name": STRUCTURED_OUTPUT_TOOL_NAME, "args": args, "id": "call",
                    }]))
            return SimpleNamespace(status="completed", structured=None,
                                   message=message, served_model="glm-5.3-flash")

        monkeypatch.setattr(runtime, "invoke_model_operation_v2", invoke)
        result = await _precheck(runtime, _profile(), host="www.cninfo.com.cn")
        assert result.decision == verdict
        assert len(calls) == 2
        assert internal.recorded_entries[0]["model"] == "glm-5.3-flash"

    @pytest.mark.asyncio
    async def test_v2_malformed_structured_tool_fails_safe(self, monkeypatch):
        runtime = _runtime_with_run(FakeInternal())
        monkeypatch.setenv("MODEL_BACKEND", "litellm")

        async def invoke(request, context):
            return SimpleNamespace(status="completed", structured=None,
                served_model="glm", message={"role": "assistant", "content": "allow",
                    "tool_calls": [{"function": {"name": "wrong_tool",
                        "arguments": '{"verdict":"allow"}'}}]})

        runtime.runs["run-1"].config["configurable"]["egress_classifier_stages"] = "thinking"
        monkeypatch.setattr(runtime, "invoke_model_operation_v2", invoke)
        assert (await _precheck(runtime, _profile())).decision == "ask"

    @pytest.mark.asyncio
    async def test_manual_baseline_asks_without_classifier(self):
        internal = FakeInternal()
        runtime = _runtime_with_run(internal)
        result = await _precheck(runtime, _profile(unknown_target="ask"))
        assert (result.decision, result.source) == ("ask", "mode")
        assert runtime.egress_classifiers == {}

    @pytest.mark.asyncio
    async def test_deny_baseline_denies(self):
        runtime = _runtime_with_run(FakeInternal())
        result = await _precheck(runtime, _profile(unknown_target="deny"))
        assert (result.decision, result.source) == ("deny", "mode")

    @pytest.mark.asyncio
    async def test_auto_baseline_classifies_allow_and_caches(self, monkeypatch):
        internal = FakeInternal()
        runtime = _runtime_with_run(internal)
        model = FakeModelOperation([AIMessage(content="allow")])
        monkeypatch.setattr(runtime, "invoke_model_operation", model)
        first = await _precheck(runtime, _profile(unknown_target="auto"))
        assert first.decision == "allow"
        assert first.source == "classifier"
        assert len(model.requests) == 1
        assert model.requests[0].role == "egress_classifier"
        second = await _precheck(
            runtime, _profile(unknown_target="auto"), host="docs.example.com"
        )
        assert (second.decision, second.source) == ("allow", "ledger")
        assert len(model.requests) == 1
        assert len(internal.recorded_entries) == 1

    @pytest.mark.asyncio
    async def test_auto_baseline_stage2_structured_deny(self, monkeypatch):
        runtime = _runtime_with_run(FakeInternal())
        model = FakeModelOperation(
            [
                AIMessage(content="deny"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": STRUCTURED_OUTPUT_TOOL_NAME,
                            "args": {
                                "verdict": "deny",
                                "category": "suspicious",
                                "risk_tags": ["phishing"],
                                "reason": "phishing kit host",
                            },
                            "id": "call-1",
                        }
                    ],
                ),
            ]
        )
        monkeypatch.setattr(runtime, "invoke_model_operation", model)
        result = await _precheck(runtime, _profile(unknown_target="auto"))
        assert (result.decision, result.source) == ("deny", "classifier")
        assert len(model.requests) == 2
        assert model.requests[1].tools[0]["function"]["name"] == (
            STRUCTURED_OUTPUT_TOOL_NAME
        )

    @pytest.mark.asyncio
    async def test_classifier_ask_falls_back_to_human(self, monkeypatch):
        runtime = _runtime_with_run(FakeInternal())
        model = FakeModelOperation(
            [
                AIMessage(content="ask"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": STRUCTURED_OUTPUT_TOOL_NAME,
                            "args": {"verdict": "ask"},
                            "id": "call-1",
                        }
                    ],
                ),
            ]
        )
        monkeypatch.setattr(runtime, "invoke_model_operation", model)
        result = await _precheck(runtime, _profile(unknown_target="auto"))
        assert (result.decision, result.source) == ("ask", "classifier")

    @pytest.mark.asyncio
    async def test_approval_policy_never_denies_classifier_ask(self, monkeypatch):
        runtime = _runtime_with_run(FakeInternal())
        model = FakeModelOperation([AIMessage(content="ask")])
        monkeypatch.setattr(runtime, "invoke_model_operation", model)
        result = await _precheck(
            runtime, _profile(unknown_target="auto", approval_policy="never")
        )
        assert result.decision == "deny"

    @pytest.mark.asyncio
    async def test_manual_and_never_denies_without_model(self, monkeypatch):
        runtime = _runtime_with_run(FakeInternal())
        result = await _precheck(
            runtime, _profile(unknown_target="ask", approval_policy="never")
        )
        assert (result.decision, result.source) == ("deny", "mode")

    @pytest.mark.asyncio
    async def test_runtime_override_narrows_auto_to_manual(self, monkeypatch):
        runtime = _runtime_with_run(FakeInternal(override={"mode": "manual"}))
        result = await _precheck(runtime, _profile(unknown_target="auto"))
        assert (result.decision, result.source) == ("ask", "mode")

    @pytest.mark.asyncio
    async def test_runtime_override_wider_than_baseline_is_capped(self):
        runtime = _runtime_with_run(FakeInternal(override={"mode": "open"}))
        result = await _precheck(runtime, _profile(unknown_target="ask"))
        assert (result.decision, result.source) == ("ask", "mode")

    @pytest.mark.asyncio
    async def test_human_allow_run_reuse_beats_classifier(self, monkeypatch):
        host = "portal.example.com"
        target = {"domain": host, "port": 443}
        fingerprint = SecurityApprovalStore.fingerprint(
            "network", "tool.egress", target
        )
        approval = {
            "approval_id": "appr-1",
            "run_id": "run-1",
            "task_id": "task-1",
            "fence_token": 1,
            "kind": "network",
            "capability": "tool.egress",
            "target": target,
            "target_fingerprint": fingerprint,
            "status": "resolved",
            "decision": "allow_run",
            "operation_id": "op-other",
            "expires_at": time.time() + 60,
        }
        internal = FakeInternal(approvals=[approval])
        runtime = _runtime_with_run(internal)
        result = await _precheck(runtime, _profile(unknown_target="auto"), host=host)
        assert (result.decision, result.source) == ("allow", "human")
        assert "/internal/sandbox/approvals/consume" not in internal.posted_paths
        assert internal.recorded_entries == []

    @pytest.mark.asyncio
    async def test_stale_fence_denies(self):
        runtime = _runtime_with_run(FakeInternal())
        result = await runtime._egress_precheck(
            run_id="run-1",
            task_id="task-1",
            fence_token=99,
            stage="researching",
            host="x.example.com",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            operation_id="op-1",
            profile=_profile(unknown_target="auto"),
        )
        assert (result.decision, result.source) == ("deny", "stale_fence")


class TestLedgerStores:
    def test_mode_store_roundtrip_and_clear(self, tmp_path):
        store = RunEgressModeStore("run-a", runs_dir=str(tmp_path))
        assert store.get() is None
        store.set(mode="auto", actor="user-1", fence_token=3)
        override = store.get()
        assert override is not None and override.mode == "auto"
        assert override.fence_token == 3
        store.clear(fence_token=3)
        assert store.get() is None

    def test_mode_store_rejects_unknown_mode(self, tmp_path):
        store = RunEgressModeStore("run-a", runs_dir=str(tmp_path))
        with pytest.raises(ValueError):
            store.set(mode="yolo", actor="u", fence_token=1)

    def test_classification_store_record_and_dedup(self, tmp_path):
        store = EgressClassificationStore("run-a", runs_dir=str(tmp_path))
        entry = EgressClassificationEntry(
            fingerprint=classification_fingerprint("docs.example.com"),
            registered_domain="example.com",
            host="docs.example.com",
            port=443,
            verdict="allow",
            source="stage2",
            tool="fetch_url",
        )
        assert store.record(entry.to_payload()) == "recorded"
        assert store.record(entry.to_payload()) == "duplicate"
        loaded = store.load()
        assert loaded[entry.fingerprint]["verdict"] == "allow"

    def test_classification_store_rejects_fingerprint_mismatch(self, tmp_path):
        store = EgressClassificationStore("run-a", runs_dir=str(tmp_path))
        payload = EgressClassificationEntry(
            fingerprint="egress:other.com",
            registered_domain="example.com",
            host="a.example.com",
            port=443,
            verdict="allow",
            source="stage1",
        ).to_payload()
        with pytest.raises(Exception):
            store.record(payload)


def _internal_test_client(tmp_path, fence_token=1):
    from fastapi import FastAPI

    configuration = _sandbox_config(runs_dir=str(tmp_path))
    context = InternalRunContext(
        config={"configurable": {"runs_dir": str(tmp_path)}},
        configurable=configuration,
        fence_token=fence_token,
        started_at=time.time(),
    )
    app = FastAPI()
    app.include_router(build_internal_sandbox_router(lambda run_id: context))
    client = TestClient(app)
    signed_client = SandboxInternalClient("http://test", ROOT_KEY)
    keys = SandboxDerivedKeys.from_root(ROOT_KEY)
    return client, signed_client, keys


def _sign(client, keys, model_type, **values):
    request = client.signed(model_type, **values)
    request.service_signature = sign_payload(
        request.signed_payload(), keys.service_auth
    )
    return request


def _post(client, path, request):
    return client.post(path, json=request.model_dump(mode="json"))


class TestInternalEgressEndpoints:
    def test_mode_get_absent_then_present(self, tmp_path):
        client, signed, keys = _internal_test_client(tmp_path)
        request = _sign(
            signed, keys, EgressModeGetRequest, run_id="run-x", fence_token=1
        )
        response = _post(client, "/internal/sandbox/egress/mode/get", request)
        assert response.status_code == 200
        assert response.json()["override"] is None
        RunEgressModeStore("run-x", runs_dir=str(tmp_path)).set(
            mode="auto", actor="user-1", fence_token=1
        )
        request = _sign(
            signed, keys, EgressModeGetRequest, run_id="run-x", fence_token=1
        )
        response = _post(client, "/internal/sandbox/egress/mode/get", request)
        override = response.json()["override"]
        assert override is not None and override["mode"] == "auto"

    def test_mode_get_stale_fence_reads_absent(self, tmp_path):
        client, signed, keys = _internal_test_client(tmp_path, fence_token=2)
        RunEgressModeStore("run-x", runs_dir=str(tmp_path)).set(
            mode="manual", actor="user-1", fence_token=1
        )
        request = _sign(
            signed, keys, EgressModeGetRequest, run_id="run-x", fence_token=2
        )
        response = _post(client, "/internal/sandbox/egress/mode/get", request)
        assert response.status_code == 200
        assert response.json()["override"] is None

    def test_classification_record_and_load_roundtrip(self, tmp_path):
        client, signed, keys = _internal_test_client(tmp_path)
        entry = EgressClassificationEntry(
            fingerprint=classification_fingerprint("docs.example.com"),
            registered_domain="example.com",
            host="docs.example.com",
            port=443,
            verdict="deny",
            source="stage2",
            tool="fetch_url",
        )
        request = _sign(
            signed,
            keys,
            EgressClassificationRecordRequest,
            run_id="run-x",
            task_id="task-1",
            fence_token=1,
            entry=entry.to_payload(),
        )
        response = _post(
            client, "/internal/sandbox/egress/classifications/record", request
        )
        assert response.status_code == 200
        assert response.json()["status"] == "recorded"
        load = _sign(
            signed, keys, EgressClassificationLoadRequest, run_id="run-x", fence_token=1
        )
        response = _post(
            client, "/internal/sandbox/egress/classifications/load", load
        )
        entries = response.json()["entries"]
        assert entries[entry.fingerprint]["verdict"] == "deny"

    def test_classification_record_rejects_invalid_entry(self, tmp_path):
        client, signed, keys = _internal_test_client(tmp_path)
        request = _sign(
            signed,
            keys,
            EgressClassificationRecordRequest,
            run_id="run-x",
            task_id="task-1",
            fence_token=1,
            entry={"fingerprint": "egress:x", "verdict": "maybe", "source": "stage1"},
        )
        response = _post(
            client, "/internal/sandbox/egress/classifications/record", request
        )
        assert response.status_code == 409
