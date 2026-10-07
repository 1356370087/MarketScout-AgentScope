"""Sandbox Policy V7 contracts, payload safety and durable control state."""

import asyncio
import base64
import io
import json
import secrets
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from agentscope.message import UserMsg
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr, ValidationError

from open_deep_research.agentscope_runtime.gateway import (
    SandboxBinding,
    SandboxChatModel,
)
from open_deep_research.agentscope_runtime.sandbox_provider import (
    STRUCTURED_OUTPUT_TOOL_NAME,
)
from open_deep_research.agentscope_runtime.sandbox_workspace import (
    ControllerBackend,
    ControllerWorkspace,
    ControllerWorkspaceSpec,
)
from open_deep_research.configuration import (
    RUN_CONFIG_FROZEN_FIELDS,
    RUN_CONFIG_FROZEN_FIELDS_V7,
    RUN_CONFIG_SCHEMA_VERSION,
    Configuration,
    freeze_run_config,
    frozen_run_config_values,
)
from open_deep_research.sandbox import doctor as sandbox_doctor
from open_deep_research.sandbox import gateway_client
from open_deep_research.sandbox.approvals import SecurityApprovalStore
from open_deep_research.sandbox.controller import DockerControllerRuntime, _tar_payload
from open_deep_research.sandbox.crypto import (
    NonceReplayCache,
    SandboxDerivedKeys,
    decode_task_token,
    encode_task_token,
    sign_payload,
)
from open_deep_research.sandbox.gateway import (
    GatewayRunContext,
    GatewayRunRegistrationRequest,
    GatewayRuntime,
    RemoteBudgetGate,
    create_gateway_app,
)
from open_deep_research.sandbox.gateway_catalog import GatewayCatalogTool
from open_deep_research.sandbox.operations import ModelOperationStore
from open_deep_research.sandbox.safe_io import (
    ArchiveLimits,
    UnsafeSandboxArchive,
    extract_safe_tar,
    repack_docker_archives,
)
from open_deep_research.sandbox.schema import (
    NetworkPolicy,
    load_policy_bundle,
    network_target_decision,
    policy_digest,
)
from open_deep_research.sandbox.wire import (
    GatewayCatalogToolV1,
    GatewayModelOutcomeV2,
    GatewayModelRequestV2,
    SandboxTaskPayloadV1,
    SandboxTaskResultV1,
    TaskTokenClaimsV1,
)
from open_deep_research.security.inputs import (
    validate_http_configurable,
    validate_http_metadata,
)
from open_deep_research.tools.base import tool_to_model_definition

ROOT_KEY = base64.b64encode(b"k" * 32).decode()


def _sandbox_config(**overrides):
    values = {
        "sandbox_enabled": True,
        "enable_async_research": True,
        "sandbox_root_signing_key": ROOT_KEY,
        "sandbox_policy_path": "config/sandbox-policy.toml",
    }
    values.update(overrides)
    return Configuration(**values)


def _tar(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = data.decode()
                bundle.addfile(info)
    return buffer.getvalue()


def test_v7_defaults_disabled_and_legacy_fields_are_rejected():
    assert RUN_CONFIG_SCHEMA_VERSION == 14
    assert Configuration().sandbox_enabled is False
    with pytest.raises(ValidationError, match="legacy_sandbox_config_removed"):
        Configuration(enable_docker_sandbox=True)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_enabled_sandbox_supports_native_scheduling_and_requires_root_key(asynchronous):
    cfg = _sandbox_config(enable_async_research=asynchronous)
    assert cfg.sandbox_enabled
    assert cfg.enable_async_research is asynchronous
    with pytest.raises(ValidationError, match="root_signing_key"):
        Configuration(sandbox_enabled=True, enable_async_research=asynchronous)


def test_per_run_api_keys_require_explicit_vault_mode(monkeypatch) -> None:
    monkeypatch.delenv("GET_API_KEYS_FROM_CONFIG", raising=False)
    with pytest.raises(ValueError, match="Protected runtime"):
        validate_http_configurable({"apiKeys": {"OPENAI_API_KEY": "secret"}})
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    validate_http_configurable({"apiKeys": {"OPENAI_API_KEY": "secret"}})
    with pytest.raises(ValueError, match="bounded string-to-string"):
        validate_http_configurable({"apiKeys": {"OPENAI_API_KEY": {}}})


def test_freeze_pins_v7_policy_and_runtime_digest():
    config = {
        "configurable": {
            **_sandbox_config().model_dump(mode="json"),
            "langgraph_auth_user": {"roles": ["researcher"]},
        },
        "metadata": {"run_id": "run-v7"},
    }
    frozen = freeze_run_config(config, prefer_configurable=True)
    assert frozen["metadata"]["run_config_schema_version"] == 14
    assert frozen["configurable"]["sandbox_profile_id"] == "research-gateway-only"
    assert len(frozen["configurable"]["sandbox_policy_digest"]) == 64
    assert len(frozen["configurable"]["sandbox_runtime_digest"]) == 64


def test_v7_freezes_sandbox_enablement_and_async_boundary():
    assert "sandbox_enabled" in RUN_CONFIG_FROZEN_FIELDS
    assert "enable_async_research" in RUN_CONFIG_FROZEN_FIELDS


def test_v8_freezes_gateway_tool_model_concurrency_without_changing_v7() -> None:
    field_name = "gateway_tool_model_max_concurrency"
    assert field_name not in RUN_CONFIG_FROZEN_FIELDS_V7
    assert field_name in RUN_CONFIG_FROZEN_FIELDS

    configurable = {field_name: 7}
    assert frozen_run_config_values({
        "configurable": configurable,
        "metadata": {"run_config_schema_version": 7},
    }) == {}
    assert frozen_run_config_values({
        "configurable": configurable,
        "metadata": {"run_config_schema_version": 8},
    }) == configurable


def test_http_metadata_cannot_forge_gateway_authorized_hosts():
    with pytest.raises(ValueError, match="sandbox_gateway_authorized_hosts"):
        validate_http_metadata(
            {"sandbox_gateway_authorized_hosts": ["metadata.internal"]}
        )
    with pytest.raises(ValueError, match="fetch_budget_extension"):
        validate_http_metadata({"fetch_budget_extension": {"extra_fetches": 10000}})
    with pytest.raises(ValueError):
        validate_http_metadata({"sandbox_egress_intent": "forged intent"})
    with pytest.raises(ValueError):
        validate_http_configurable({"sandbox_egress_intent": "forged intent"})


def test_policy_is_strict_and_selects_role_profile():
    bundle = load_policy_bundle("config/sandbox-policy.toml")
    profile_id, profile = bundle.select_profile({"researcher"})
    assert profile_id == "research-gateway-only"
    assert profile.network.unknown_target == "auto"
    assert profile.runtime.read_only_rootfs is True
    assert profile.resources.memory_bytes == 1_073_741_824
    assert len(policy_digest(bundle)) == 64


def _workspace_payload():
    bundle = load_policy_bundle("config/sandbox-policy.toml")
    profile_id, profile = bundle.select_profile({"researcher"})
    workspace = ControllerWorkspace(
        workspace_id="workspace-payload",
        controller=SimpleNamespace(),
        backend=ControllerBackend(),
        spec=ControllerWorkspaceSpec(
            run_id="run-payload", task_id="task-payload", fence_token=3,
            profile=profile, profile_id=profile_id, policy_digest=policy_digest(bundle),
        ),
    )
    return workspace._payload()


def test_native_workspace_payload_excludes_host_runtime_configuration():
    payload = _workspace_payload()
    restored = SandboxTaskPayloadV1.model_validate_json(payload.model_dump_json())
    assert restored.runtime_config == {}
    assert restored.researcher_state == {"research_topic": "workspace"}
    assert restored.fence_token == 3
    assert restored.run_id == "run-payload"
    assert restored.task_id == "task-payload"


def test_payload_rejects_checkpoint_callback():
    data = _workspace_payload().model_dump()
    data["researcher_state"]["_query_checkpoint_callback"] = lambda _: None
    with pytest.raises(ValidationError, match="unsupported keys"):
        SandboxTaskPayloadV1.model_validate(data)


def test_payload_runtime_never_inherits_configuration_credentials() -> None:
    sensitive_suffixes = (
        "_api_key", "_secret_key", "_access_token", "_auth_token", "_password",
    )
    sensitive_fields = {
        name for name in Configuration.model_fields
        if name in {"mcp_subject_token", "apiKeys", "sandbox_root_signing_key"}
        or name.endswith(sensitive_suffixes)
    }
    assert sensitive_fields
    assert sensitive_fields.isdisjoint(_workspace_payload().runtime_config)


@pytest.mark.parametrize("key", ["apiKeys", "researcher_api_key", "langfuse_secret_key"])
def test_payload_rejects_explicit_runtime_credentials(key):
    data = _workspace_payload().model_dump()
    data["runtime_config"][key] = "must-not-reach-worker"
    with pytest.raises(ValidationError, match="credential-shaped keys"):
        SandboxTaskPayloadV1.model_validate(data)


def test_complete_result_preserves_evidence_contract():
    result = SandboxTaskResultV1(
        task_id="task-result",
        status="completed",
        compressed_research="summary",
        candidate_registry=[{"url": "https://example.com"}],
        document_registry=[{"document_id": "d1"}],
        evidence_registry=[{"evidence_id": "e1"}],
        web_research_iterations=[{"iteration": 1}],
    )
    restored = SandboxTaskResultV1.model_validate_json(result.model_dump_json())
    assert restored.evidence_registry == [{"evidence_id": "e1"}]


@pytest.mark.asyncio
async def test_native_compression_transport_preserves_requirement_ids_and_binding():
    requirement = "COV-01-a1b2c3d4e5f6"
    requests = []

    def respond(request):
        assert request.url.path == "/v2/models/complete"
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=GatewayModelOutcomeV2(
            logical_operation_id=body["logical_operation_id"],
            requested_model=body["model"], status="completed", finish_reason="stop",
            message={"role": "assistant", "content": f"Supported: {requirement}"},
        ).model_dump(mode="json"))

    async with httpx.AsyncClient(
        base_url="http://gateway", transport=httpx.MockTransport(respond),
    ) as client:
        model = SandboxChatModel(
            binding=SandboxBinding(
                url="http://gateway", run_id="run-role", task_id="task-role",
                role="compression", stage="synthesizing", token=SecretStr("task-token"),
            ),
            model="compression-model", client=client, stream=True,
        )
        stream = await model([UserMsg("user", f"Owned requirement {requirement}")])
        responses = [response async for response in stream]
    assert len(responses) == 1
    assert responses[0].is_last
    assert requirement in responses[0].content[0].text
    assert len(requests) == 1
    request = requests[0]
    assert requirement in json.dumps(request["messages"])
    assert request["role"] == "compression"
    assert request["stage"] == "synthesizing"
    assert request["run_id"] == "run-role"
    assert request["task_id"] == "task-role"


def test_controller_payload_is_readable_by_unprivileged_worker():
    archive = _tar_payload("task_payload.json", b"{}", uid=65532, gid=65532)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        member = bundle.getmember("task_payload.json")
    assert member.uid == 65532
    assert member.gid == 65532
    assert member.mode == 0o444


def test_controller_detects_result_ready_before_tmpfs_is_unmounted():
    class Container:
        @staticmethod
        def exec_run(command, **_kwargs):
            return SimpleNamespace(
                exit_code=0,
                output=(b"81a4:1" if command[0] == "/usr/bin/stat" else b"0"),
            )

    assert DockerControllerRuntime._ready_exit_code(Container()) == 0


def test_safe_tar_rejects_links_and_traversal(tmp_path):
    with pytest.raises(UnsafeSandboxArchive, match="special entry"):
        extract_safe_tar(
            _tar([("leak", b"/etc/passwd", "symlink")]),
            tmp_path,
            limits=ArchiveLimits(max_bytes=1024),
        )
    with pytest.raises(UnsafeSandboxArchive, match="unsafe archive path"):
        extract_safe_tar(
            _tar([("../escape", b"bad", "file")]),
            tmp_path,
            limits=ArchiveLimits(max_bytes=1024),
        )


def test_safe_tar_enforces_bytes_and_writes_regular_file(tmp_path):
    archive = _tar([("output/result.txt", b"hello", "file")])
    files = extract_safe_tar(
        archive,
        tmp_path,
        limits=ArchiveLimits(max_bytes=5, max_files=1),
    )
    assert files[0].read_text() == "hello"
    with pytest.raises(UnsafeSandboxArchive, match="bytes exceed"):
        extract_safe_tar(
            archive,
            tmp_path / "second",
            limits=ArchiveLimits(max_bytes=4),
        )


def test_task_token_and_nonce_replay_contract():
    keys = SandboxDerivedKeys.from_root(ROOT_KEY)
    claims = TaskTokenClaimsV1(
        run_id="run-token",
        task_id="task-token",
        fence_token=7,
        profile_id="research-gateway-only",
        policy_digest="a" * 64,
        issued_at=time.time(),
        expires_at=time.time() + 60,
        jti="jti-1",
    )
    restored = decode_task_token(encode_task_token(claims, keys.task_token), keys.task_token)
    assert restored.fence_token == 7
    cache = NonceReplayCache()
    nonce = "nonce-at-least-16-bytes"
    cache.consume("jti-1", nonce, expires_at=claims.expires_at)
    with pytest.raises(ValueError, match="nonce_replayed"):
        cache.consume("jti-1", nonce, expires_at=claims.expires_at)


def test_gateway_accepts_service_signed_api_model_request() -> None:
    configurable = _sandbox_config()
    runtime = GatewayRuntime(configurable)
    now = time.time()
    registration = GatewayRunRegistrationRequest(
        run_id="run-service-model",
        fence_token=3,
        frozen_config={
            "configurable": configurable.model_dump(mode="json"),
            "metadata": {"run_config_fingerprint": "frozen"},
        },
        api_keys={},
        expires_at=now + 60,
        service_timestamp=now,
        service_nonce=secrets.token_urlsafe(24),
        service_signature="pending",
    )
    registration.service_signature = sign_payload(
        registration.signed_payload(),
        runtime.keys.service_auth,
    )
    runtime.register(registration)
    request = GatewayModelRequestV2(
        model="supervisor-model",
        run_id="run-service-model",
        task_id="api",
        role="supervisor",
        stage="planning",
        logical_operation_id="logical-api-model",
        messages=[],
    )
    timestamp = time.time()
    nonce = secrets.token_urlsafe(24)
    signature = sign_payload(
        {
            "request": request.model_dump(mode="json"),
            "timestamp": timestamp,
            "nonce": nonce,
            "fence_token": 3,
        },
        runtime.keys.service_auth,
    )
    context = runtime.authorize_api_model(
        request,
        timestamp=timestamp,
        nonce=nonce,
        fence_token=3,
        signature=signature,
    )
    assert context.fence_token == 3
    with pytest.raises(ValueError, match="nonce_replayed"):
        runtime.authorize_api_model(
            request,
            timestamp=timestamp,
            nonce=nonce,
            fence_token=3,
            signature=signature,
        )


@pytest.mark.asyncio
async def test_gateway_unregister_retries_with_fresh_nonces(monkeypatch) -> None:
    attempts: list[dict] = []

    class AsyncClient:
        def __init__(self, **_kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, content, headers):
            del headers
            attempts.append(json.loads(content))
            status = 503 if len(attempts) < 3 else 200
            return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr(gateway_client.httpx, "AsyncClient", AsyncClient)
    client = gateway_client.SandboxGatewayControlClient(_sandbox_config())

    await client.unregister_run(run_id="run-retry", fence_token=7)
    assert len(attempts) == 3
    assert len({item["service_nonce"] for item in attempts}) == 3


def test_gateway_registration_collects_only_allowed_environment_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "false")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-secret")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "firecrawl-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")

    frozen, credentials = gateway_client.split_gateway_registration(
        {"configurable": {"research_model": "if-research-v1"}}
    )

    assert credentials == {
        "TAVILY_API_KEY": "tavily-secret",
        "FIRECRAWL_API_KEY": "firecrawl-secret",
    }
    assert "apiKeys" not in frozen["configurable"]


def test_gateway_registration_prefers_config_credentials_in_oap_mode(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "environment-secret")

    _frozen, credentials = gateway_client.split_gateway_registration(
        {
            "configurable": {
                "apiKeys": {"TAVILY_API_KEY": "per-run-secret"},
            }
        }
    )

    assert credentials == {"TAVILY_API_KEY": "per-run-secret"}


def test_gateway_expiry_wipes_run_credentials_and_operation_locks() -> None:
    runtime = GatewayRuntime(_sandbox_config())
    now = time.time()
    registration = GatewayRunRegistrationRequest(
        run_id="run-expiring",
        fence_token=4,
        frozen_config={"configurable": {}, "metadata": {}},
        api_keys={"OPENAI_API_KEY": "ephemeral-secret"},
        expires_at=now + 1,
        service_timestamp=now,
        service_nonce=secrets.token_urlsafe(24),
        service_signature="pending",
    )
    registration.service_signature = sign_payload(
        registration.signed_payload(), runtime.keys.service_auth
    )
    runtime.register(registration)
    context = runtime.runs["run-expiring"]
    runtime.operation_locks[("run-expiring", "operation")] = asyncio.Lock()

    assert runtime.evict_expired_runs(now=now + 2) == ["run-expiring"]
    assert "run-expiring" not in runtime.runs
    assert not any(key[0] == "run-expiring" for key in runtime.operation_locks)
    assert context.api_keys == {}
    assert context.config == {}


def test_gateway_same_fence_registration_preserves_run_key() -> None:
    runtime = GatewayRuntime(_sandbox_config())
    now = time.time()

    def registration(api_keys: dict[str, str]) -> GatewayRunRegistrationRequest:
        request = GatewayRunRegistrationRequest(
            run_id="run-key-refresh",
            fence_token=4,
            frozen_config={"configurable": {}, "metadata": {}},
            api_keys=api_keys,
            expires_at=now + 60,
            service_timestamp=time.time(),
            service_nonce=secrets.token_urlsafe(24),
            service_signature="pending",
        )
        request.service_signature = sign_payload(
            request.signed_payload(), runtime.keys.service_auth
        )
        return request

    runtime.register(registration({"LITELLM_RUN_KEY": "run-secret"}))
    runtime.runs["run-key-refresh"].config["metadata"]["fetch_budget_extension"] = {
        "extra_fetches": 40, "granted_at": now,
    }
    runtime.register(registration({}))

    assert runtime.runs["run-key-refresh"].api_keys == {
        "LITELLM_RUN_KEY": "run-secret"
    }
    assert runtime.runs["run-key-refresh"].config["metadata"]["fetch_budget_extension"]["extra_fetches"] == 40


@pytest.mark.asyncio
async def test_gateway_v2_missing_run_key_does_not_touch_journal_or_budget() -> None:
    runtime = GatewayRuntime(_sandbox_config())

    class NoInternalCalls:
        def signed(self, *_args, **_kwargs):
            raise AssertionError("missing credentials must fail before journal access")

    runtime.internal = NoInternalCalls()  # type: ignore[assignment]
    request = GatewayModelRequestV2(
        run_id="run-no-key",
        task_id="task-no-key",
        role="researcher",
        stage="researching",
        logical_operation_id="model:no-key",
        model="if-research-v1",
        messages=[{"role": "user", "content": "question"}],
    )
    context = GatewayRunContext(
        config={"configurable": {}, "metadata": {}},
        fence_token=1,
        expires_at=time.time() + 60,
    )

    with pytest.raises(RuntimeError, match="sandbox_gateway_run_key_unavailable"):
        await runtime.invoke_model_operation_v2(request, context)


@pytest.mark.asyncio
async def test_gateway_v2_persists_structured_payload_for_replay() -> None:
    runtime = GatewayRuntime(_sandbox_config())
    saved = {}
    invocations = []

    class Internal:
        def signed(self, _kind, **payload):
            return SimpleNamespace(**payload)

        async def post(self, path, request):
            if path.endswith("operations/get"):
                return {"found": bool(saved), "operation": saved}
            if path.endswith("operations/transition") and request.status == "completed":
                saved.update(status="completed", outcome=request.outcome)
            return {"status": "ok"}

    class Gateway:
        async def complete(self, request):
            invocations.append(request)
            assert request.tool_choice == {
                "type": "function", "function": {"name": STRUCTURED_OUTPUT_TOOL_NAME},
            }
            assert request.tools[0]["function"]["parameters"] == request.structured_schema
            return GatewayModelOutcomeV2(
                logical_operation_id=request.logical_operation_id,
                requested_model=request.model, status="completed",
                message={"role": "assistant", "content": "reviewed", "tool_calls": [{
                    "id": "call", "type": "function", "function": {
                        "name": STRUCTURED_OUTPUT_TOOL_NAME,
                        "arguments": json.dumps({"verdict": "allow"}),
                    },
                }]},
                usage={"input_tokens": 2, "output_tokens": 3}, finish_reason="tool_calls",
            )

    runtime.internal = Internal()
    runtime.model_gateways["structured-run"] = Gateway()
    context = GatewayRunContext(config={"configurable": {}, "metadata": {}},
        fence_token=1, expires_at=time.time() + 60,
        api_keys={"LITELLM_RUN_KEY": "test-key"})
    request = GatewayModelRequestV2(run_id="structured-run", task_id="task",
        role="egress_classifier", stage="researching", logical_operation_id="classify",
        model="classifier", messages=[{"role": "user", "content": "review"}],
        structured_schema={"type": "object", "properties": {"verdict": {"type": "string"}}})
    first = await runtime.invoke_model_operation_v2(request, context)
    replay = await runtime.invoke_model_operation_v2(request, context)
    assert first.structured == replay.structured == {"verdict": "allow"}
    assert len(invocations) == 1
    assert saved["outcome"]["structured"] == {"verdict": "allow"}


@pytest.mark.asyncio
async def test_gateway_v2_cancellation_releases_budget_and_journals_uncertain() -> None:
    """A Worker killed mid-dispatch must not leak the reservation or stick at
    "dispatched" for replays of the same logical operation."""
    runtime = GatewayRuntime(_sandbox_config())
    posted: list[tuple[str, object]] = []

    class RecordingInternal:
        def signed(self, _kind, **payload):
            return SimpleNamespace(**payload)

        async def post(self, path, request):
            posted.append((path, getattr(request, "status", None)))
            if path == "/internal/sandbox/operations/get":
                return {"found": False}
            return {"status": "ok"}

    runtime.internal = RecordingInternal()  # type: ignore[assignment]

    class CancellingGateway:
        async def complete(self, _request):
            raise asyncio.CancelledError()

    runtime.model_gateways["run-cancel"] = CancellingGateway()  # type: ignore[assignment]
    runtime.runs["run-cancel"] = GatewayRunContext(
        config={"configurable": {}, "metadata": {}},
        fence_token=1,
        expires_at=time.time() + 60,
        api_keys={"LITELLM_RUN_KEY": "run-secret"},
    )
    request = GatewayModelRequestV2(
        run_id="run-cancel",
        task_id="task-cancel",
        role="researcher",
        stage="researching",
        logical_operation_id="model:cancelled-op",
        model="if-research-v1",
        messages=[{"role": "user", "content": "question"}],
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime.invoke_model_operation_v2(request, runtime.runs["run-cancel"])

    paths = [path for path, _status in posted]
    assert paths == [
        "/internal/sandbox/operations/get",
        "/internal/sandbox/budgets/reserve",
        "/internal/sandbox/operations/transition",
        "/internal/sandbox/task-activity",
        "/internal/sandbox/budgets/fail",
        "/internal/sandbox/operations/transition",
    ]
    statuses = [status for _path, status in posted]
    # Journal: dispatched on reserve, then a definite uncertain terminal state.
    assert statuses == [None, None, "dispatched", "running", None, "uncertain"]


def test_gateway_app_lifespan_reaps_expired_credentials_without_api_cleanup() -> None:
    runtime = GatewayRuntime(_sandbox_config())
    now = time.time()
    registration = GatewayRunRegistrationRequest(
        run_id="run-background-expiry",
        fence_token=2,
        frozen_config={"configurable": {}, "metadata": {}},
        api_keys={"OPENAI_API_KEY": "background-secret"},
        expires_at=now + 60,
        service_timestamp=now,
        service_nonce=secrets.token_urlsafe(24),
        service_signature="pending",
    )
    registration.service_signature = sign_payload(
        registration.signed_payload(), runtime.keys.service_auth
    )
    runtime.register(registration)
    runtime.runs["run-background-expiry"].expires_at = 0

    with TestClient(
        create_gateway_app(runtime, credential_sweep_seconds=0.01)
    ):
        deadline = time.monotonic() + 0.5
        while "run-background-expiry" in runtime.runs and time.monotonic() < deadline:
            time.sleep(0.01)

    assert "run-background-expiry" not in runtime.runs


def test_doctor_warns_when_developer_profile_is_mapped_off_linux(monkeypatch) -> None:
    bundle = SimpleNamespace(
        profile_by_role={"developer": "developer-workspace"}
    )
    monkeypatch.setattr(sandbox_doctor.sys, "platform", "win32")

    assert sandbox_doctor._developer_profile_warnings(bundle) == [
        (
            "developer-workspace is mapped but the current platform is not Linux; "
            "this profile is not release-qualified"
        )
    ]


@pytest.mark.asyncio
async def test_native_gateway_model_sends_pydantic_structured_output_schema() -> None:
    class StructuredResult(BaseModel):
        answer: str

    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=GatewayModelOutcomeV2(
            logical_operation_id=body["logical_operation_id"],
            requested_model=body["model"], status="completed", finish_reason="tool_calls",
            structured={"answer": "reviewed"},
        ).model_dump(mode="json"))

    async with httpx.AsyncClient(
        base_url="http://gateway", transport=httpx.MockTransport(respond),
    ) as client:
        model = SandboxChatModel(
            binding=SandboxBinding(
                url="http://gateway", run_id="run-structured", task_id="task-structured",
                role="researcher", stage="researching", token=SecretStr("task-token"),
            ),
            model="research-model", client=client,
        )
        response = await model.generate_structured_output(
            [UserMsg("user", "Return a structured answer")], StructuredResult,
        )
    assert response.content == {"answer": "reviewed"}
    assert len(requests) == 1
    assert requests[0]["structured_schema"] == StructuredResult.model_json_schema()
    assert requests[0]["tools"] == []


@pytest.mark.asyncio
async def test_remote_budget_gate_uses_async_internal_client(monkeypatch) -> None:
    posts: list[str] = []

    class Internal:
        base_url = "http://api"

        @staticmethod
        def signed(_model, **_kwargs):
            return SimpleNamespace(model_dump_json=lambda: "{}")

        @staticmethod
        async def post(path, _request):
            posts.append(path)
            return {}

    monkeypatch.setattr(
        "open_deep_research.sandbox.gateway.httpx.Client",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("synchronous httpx.Client used on Gateway event loop")
        ),
    )
    gate = RemoteBudgetGate(
        internal=Internal(),
        run_id="run-async-budget",
        task_id="task-async-budget",
        fence_token=1,
        stage="researching",
        logical_operation_id="logical-async-budget",
    )

    gate.reserve_model_call(
        "attempt-1",
        estimated_input_tokens=10,
        estimated_output_tokens=10,
        model_name="openai:test",
    )
    await gate.flush_pending()

    assert posts == [
        "/internal/sandbox/budgets/reserve",
        "/internal/sandbox/operations/transition",
    ]


@pytest.mark.asyncio
async def test_gateway_catalog_preserves_remote_dynamic_schema() -> None:
    remote = GatewayCatalogTool(
        GatewayCatalogToolV1(
            name="ExternalMCPTool",
            definition={
                "name": "ExternalMCPTool",
                "description": "Trusted dynamic schema",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            origin="mcp",
            effect="read_only",
        )
    )
    definition = await tool_to_model_definition(remote)
    assert definition["parameters"]["required"] == ["query"]


def test_gateway_v2_endpoint_returns_native_complete_result() -> None:
    class FakeRuntime:
        def __init__(self):
            self.runs = {}

        def authorize_task(self, request, **_kwargs):
            return object(), object()

        async def invoke_model_operation_v2(self, request, _context):
            return GatewayModelOutcomeV2(
                logical_operation_id=request.logical_operation_id,
                status="completed",
                requested_model=request.model,
                message={"role": "assistant", "content": "done"},
            )

    client = TestClient(create_gateway_app(FakeRuntime()))
    response = client.post(
        "/v2/models/complete",
        json={
            "run_id": "run-stream",
            "task_id": "task-stream",
            "role": "researcher",
            "stage": "researching",
            "logical_operation_id": "logical-stream",
            "model": "research-model",
            "messages": [],
        },
        headers={
            "Authorization": "Bearer fake",
            "X-Sandbox-Timestamp": str(time.time()),
            "X-Sandbox-Nonce": "nonce-at-least-16-bytes",
        },
    )
    assert response.status_code == 200
    outcome = GatewayModelOutcomeV2.model_validate(response.json())
    assert outcome.status == "completed"
    assert outcome.message == {"role": "assistant", "content": "done"}
    assert outcome.logical_operation_id == "logical-stream"
    assert client.post("/v1/models/stream", json={}).status_code == 404


def test_network_policy_applies_deny_before_wildcard_allow() -> None:
    policy = NetworkPolicy(
        mode="allowlist",
        allow_domains=["**.example.com"],
        deny_domains=["private.example.com"],
        allow_ports=[443],
        unknown_target="ask",
    )
    assert network_target_decision(policy, "api.example.com", 443) == "allow"
    assert network_target_decision(policy, "private.example.com", 443) == "deny"
    assert network_target_decision(policy, "unknown.test", 443) == "ask"
    assert network_target_decision(policy, "api.example.com", 80) == "deny"


def test_repack_docker_archives_rejects_links_and_canonicalizes_paths() -> None:
    output = _tar([("output/result.json", b"{}", "file")])
    packed = repack_docker_archives(
        [("output", output)],
        limits=ArchiveLimits(max_bytes=10, max_files=2),
    )
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:") as archive:
        assert archive.getnames() == ["output/result.json"]
        assert archive.getmember("output/result.json").mode == 0o600
    linked = _tar([("logs/leak", b"/proc/self/environ", "symlink")])
    with pytest.raises(UnsafeSandboxArchive, match="special entry"):
        repack_docker_archives(
            [("logs", linked)],
            limits=ArchiveLimits(max_bytes=100),
        )


def test_security_approval_is_concurrent_durable_and_allow_once_is_consumed(tmp_path):
    store = SecurityApprovalStore("run-approval", runs_dir=str(tmp_path))
    first = store.request(
        task_id="task-a",
        fence_token=4,
        kind="network",
        capability="tool.egress",
        target={"domain": "example.com", "port": 443},
        operation_id="op-a",
        expires_at=time.time() + 60,
    )
    second = store.request(
        task_id="task-b",
        fence_token=4,
        kind="command",
        capability="shell.execute",
        target={"command": "pytest"},
        operation_id="op-b",
        expires_at=time.time() + 60,
    )
    assert len(store.list(status="pending")[1]) == 2
    resolved = store.resolve(
        first.approval_id,
        decision="allow_once",
        actor="user-1",
        reason="needed",
        expected_fence_token=4,
    )
    assert resolved.decision == "allow_once"
    consumed = store.consume(
        first.approval_id,
        operation_id="op-a",
        expected_fence_token=4,
    )
    assert consumed.status == "consumed"
    assert second.status == "pending"


def test_allow_once_is_consumed_by_exactly_one_concurrent_caller(tmp_path):
    store = SecurityApprovalStore("run-consume-race", runs_dir=str(tmp_path))
    approval = store.request(
        task_id="task-race",
        fence_token=2,
        kind="network",
        capability="tool.egress",
        target={"domain": "example.com", "port": 443},
        operation_id="operation-race",
        expires_at=time.time() + 60,
    )
    store.resolve(
        approval.approval_id,
        decision="allow_once",
        actor="user-1",
        reason="race",
        expected_fence_token=2,
    )

    def consume():
        try:
            return store.consume(
                approval.approval_id,
                operation_id="operation-race",
                expected_fence_token=2,
            ).status
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: consume(), range(2)))

    assert outcomes.count("consumed") == 1
    assert outcomes.count("security_approval_not_usable") == 1


def test_model_operation_journal_accepts_multiple_physical_attempts(tmp_path):
    store = ModelOperationStore("run-op", runs_dir=str(tmp_path))
    first = store.reserve(
        task_id="task-op",
        stage="researching",
        logical_operation_id="logical-1",
        physical_attempt_id="physical-1",
    )
    assert first.status == "reserved"
    store.transition("logical-1", expected={"reserved"}, status="dispatched")
    retry = store.reserve(
        task_id="task-op",
        stage="researching",
        logical_operation_id="logical-1",
        physical_attempt_id="physical-2",
    )
    assert retry.physical_attempts == ["physical-1", "physical-2"]
    terminal = store.transition(
        "logical-1",
        expected={"dispatched"},
        status="completed",
        outcome={"status": "completed"},
    )
    assert terminal.status == "completed"


def test_model_operation_journal_retries_only_transient_failures(tmp_path):
    store = ModelOperationStore("run-op-retry", runs_dir=str(tmp_path))
    store.reserve(
        task_id="task-op",
        stage="researching",
        logical_operation_id="logical-retry",
        physical_attempt_id="physical-1",
    )
    store.transition(
        "logical-retry",
        expected={"reserved"},
        status="dispatched",
    )
    store.transition(
        "logical-retry",
        expected={"dispatched"},
        status="failed",
        outcome={"status": "failed", "error_type": "rate_limited"},
        error_type="rate_limited",
    )

    retry = store.reserve(
        task_id="task-op",
        stage="researching",
        logical_operation_id="logical-retry",
        physical_attempt_id="physical-2",
    )
    assert retry.status == "reserved"
    assert retry.outcome is None
    assert retry.physical_attempts == ["physical-1", "physical-2"]

    store.transition(
        "logical-retry",
        expected={"reserved"},
        status="dispatched",
    )
    store.transition(
        "logical-retry",
        expected={"dispatched"},
        status="failed",
        outcome={"status": "failed", "error_type": "auth"},
        error_type="auth",
    )
    with pytest.raises(ValueError, match="model_operation_already_terminal"):
        store.reserve(
            task_id="task-op",
            stage="researching",
            logical_operation_id="logical-retry",
            physical_attempt_id="physical-3",
        )
