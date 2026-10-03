"""AS-A031/032: 控制器工作区适配与出网权限网关桥接。"""

from __future__ import annotations

import ast
import asyncio
import base64
import pathlib
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from types import SimpleNamespace

import pytest

from open_deep_research.agentscope_runtime.sandbox_policy import (
    CapabilityTokenIssuer,
    EgressAuthority,
    EgressModeBridge,
    assert_worker_secret_scope,
)
from open_deep_research.agentscope_runtime.sandbox_workspace import (
    WORKSPACE_ROOT,
    ControllerBackend,
    ControllerWorkspace,
    ControllerWorkspaceManager,
    ControllerWorkspaceSpec,
)
from open_deep_research.sandbox.approvals import SecurityApprovalStore
from open_deep_research.sandbox.crypto import SandboxDerivedKeys
from open_deep_research.sandbox.egress_classifier import (
    EgressClassifier,
    EgressClassifierLimits,
)
from open_deep_research.sandbox.egress_ledger_store import RunEgressModeStore
from open_deep_research.sandbox.schema import (
    NetworkPolicy,
    ResourcePolicy,
    RuntimePolicy,
    SandboxProfile,
    runtime_digest,
)
from open_deep_research.sandbox.wire import SandboxTaskPayloadV1

pytestmark = pytest.mark.asyncio

ROOT_KEY = base64.b64encode(b"fixture-root-key-32-bytes-long!!!x").decode()


def _profile() -> SandboxProfile:
    return SandboxProfile(
        provider="docker",
        resources=ResourcePolicy(memory_bytes=512 * 1024 * 1024, cpu_cores=1.0),
        runtime=RuntimePolicy(
            worker_image_digest="sha256:" + "a" * 64,
        ),
    )


# --------------------------------------------------------------- T031 fakes


@dataclass
class FakeController:
    keys: SandboxDerivedKeys = field(default_factory=lambda: SandboxDerivedKeys.from_root(ROOT_KEY))
    created: list = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    archive_bytes: bytes = b"tar-bytes"
    next_id: int = 0

    async def create_task(self, *, payload, task_token, runtime_digest_value):
        self.created.append(
            {"payload": payload, "token": task_token, "digest": runtime_digest_value}
        )
        self.next_id += 1
        return SimpleNamespace(container_id=f"container-{self.next_id}")

    async def start_task(self, container_id):
        self.started.append(container_id)

    async def collect_archive(self, container_id):
        self.archived.append(container_id)
        return self.archive_bytes

    async def stop_task(self, container_id, *, timeout_seconds=5):
        self.stopped.append(container_id)


def _spec(task_id="task-1") -> ControllerWorkspaceSpec:
    return ControllerWorkspaceSpec(
        run_id="run-1",
        task_id=task_id,
        fence_token=3,
        profile=_profile(),
        profile_id="research",
        policy_digest="digest-1",
    )


def _backend_hooks():
    executed, read, written = [], {}, {}

    async def exec_dispatch(command, cwd, timeout):
        executed.append((command, cwd, timeout))
        from agentscope.tool._builtin._backend import ExecResult

        return ExecResult(exit_code=0, stdout=b"ok", stderr=b"")

    async def do_read(path):
        return read.get(path) or written[path]

    async def do_write(path, data):
        written[path] = data

    return ControllerBackend(exec_dispatch=exec_dispatch, read=do_read, write=do_write), {
        "executed": executed,
        "read": read,
        "written": written,
    }


def _manager(controller, tmp_path, *, spec=None, backend=None):
    return ControllerWorkspaceManager(
        controller_factory=lambda: controller,
        spec_builder=lambda user, agent, ws: dc_replace(spec or _spec(), task_id=ws),
        backend_factory=lambda ws: backend or _backend_hooks()[0],
        artifact_root=tmp_path / "artifacts",
    )


# ------------------------------------------------------- T031 生命周期


async def test_workspace_lifecycle_path_root_and_artifacts(tmp_path):
    controller = FakeController()
    backend, _hooks = _backend_hooks()
    manager = _manager(controller, tmp_path, spec=_spec(), backend=backend)
    workspace = await manager.get_workspace("user", "agent-1", "session")
    assert workspace.container_id == "container-1"
    # create 携带绑定五元组的载荷与 profile 运行时摘要；start 随后执行。
    created = controller.created[0]
    assert created["payload"].run_id == "run-1"
    assert created["payload"].profile_id == "research"
    assert created["payload"].policy_digest == "digest-1"
    assert created["digest"] == runtime_digest(_profile())
    # 任务令牌经能力令牌签发器签出并可回验。
    claims = CapabilityTokenIssuer(controller.keys).verify(created["token"])
    assert claims.run_id == "run-1"
    assert claims.fence_token == 3
    # 路径根固定为容器内 /workspace/work。
    assert workspace.workdir == WORKSPACE_ROOT
    instructions = await workspace.get_instructions()
    assert WORKSPACE_ROOT in instructions and "memory_bytes" in instructions
    # close：先收集工件（落盘）再停止容器。
    await manager.close_all()
    assert controller.archived == ["container-1"]
    assert controller.stopped == ["container-1"]
    archive = tmp_path / "artifacts" / workspace.workspace_id / f"{workspace.workspace_id}.tar"
    assert archive.read_bytes() == b"tar-bytes"
    assert workspace.container_id is None
    # 幂等 close。
    await workspace.close()
    assert controller.stopped == ["container-1"]


def _fail_first_stop(controller):
    original = controller.stop_task
    failed = False

    async def stop(container_id):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("fixture cleanup failure")
        await original(container_id)

    controller.stop_task = stop


@pytest.mark.parametrize("stop_fails", [False, True])
async def test_workspace_cancelled_close_stops_and_preserves_retry(tmp_path, stop_fails):
    controller = FakeController()
    manager = _manager(controller, tmp_path)
    workspace = await manager.get_workspace("user", "agent", "session")
    ready = asyncio.Event()
    original_collect = controller.collect_archive

    async def blocked_archive(container_id):
        ready.set()
        await asyncio.Event().wait()

    controller.collect_archive = blocked_archive
    if stop_fails:
        _fail_first_stop(controller)
    task = asyncio.create_task(manager.close(workspace.workspace_id))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert workspace.container_id == ("container-1" if stop_fails else None)
    assert controller.stopped == ([] if stop_fails else ["container-1"])
    controller.collect_archive = original_collect
    await manager.close_all()
    assert controller.stopped == ["container-1"]
    assert workspace.container_id is None
    assert not manager._workspaces


@pytest.mark.parametrize("stop_fails", [False, True])
async def test_workspace_archive_write_error_still_stops(tmp_path, stop_fails):
    controller = FakeController()
    manager = _manager(controller, tmp_path)
    workspace = await manager.get_workspace("user", "agent", "session")
    artifact_file = tmp_path / "not-directory"
    artifact_file.write_text("file")
    workspace.artifact_dir = artifact_file
    if stop_fails:
        _fail_first_stop(controller)
    with pytest.raises(FileExistsError):
        await manager.close(workspace.workspace_id)
    assert workspace.container_id == ("container-1" if stop_fails else None)
    assert controller.stopped == ([] if stop_fails else ["container-1"])
    workspace.artifact_dir = None
    await manager.close_all()
    assert controller.stopped == ["container-1"]
    assert not manager._workspaces


@pytest.mark.parametrize("stop_fails", [False, True])
@pytest.mark.parametrize("error_type", [TimeoutError, asyncio.CancelledError])
async def test_workspace_failed_start_cleans_created_container(tmp_path, stop_fails, error_type):
    controller = FakeController()
    manager = _manager(controller, tmp_path)

    async def failed_start(container_id):
        raise error_type("fixture startup failure")

    controller.start_task = failed_start
    if stop_fails:
        _fail_first_stop(controller)
    with pytest.raises(error_type, match="fixture startup failure"):
        await manager.get_workspace("user", "agent", "session", workspace_id="failed-start")
    assert len(controller.created) == 1
    assert controller.stopped == ([] if stop_fails else ["container-1"])
    if stop_fails:
        retained = manager._workspaces["failed-start"]
        assert retained.container_id == "container-1"
        assert not retained.is_alive
    await manager.close_all()
    assert controller.stopped == ["container-1"]
    assert not manager._workspaces


async def test_workspace_stop_failure_keeps_manager_cleanup_entry(tmp_path):
    controller = FakeController()
    manager = _manager(controller, tmp_path)
    workspace = await manager.get_workspace("user", "agent", "session")
    _fail_first_stop(controller)
    with pytest.raises(RuntimeError, match="fixture cleanup failure"):
        await manager.close(workspace.workspace_id)
    assert workspace.container_id == "container-1"
    assert manager._workspaces[workspace.workspace_id] is workspace
    await manager.close_all()
    assert controller.stopped == ["container-1"]
    assert not workspace.is_alive
    assert not manager._workspaces


async def test_manager_reuses_workspace_per_agent(tmp_path):
    controller = FakeController()
    backend, _ = _backend_hooks()
    manager = _manager(controller, tmp_path, spec=_spec(), backend=backend)
    first = await manager.get_workspace("user", "agent-1", "session-a")
    second = await manager.get_workspace("user", "agent-1", "session-b")
    other = await manager.get_workspace("user", "agent-2", "session-a")
    assert first is second  # PER_AGENT 隔离
    assert first is not other
    assert len(controller.created) == 2


async def test_offloader_roundtrip():
    controller = FakeController()
    backend, hooks = _backend_hooks()
    workspace = ControllerWorkspace(
        workspace_id="ws-1",
        controller=controller,
        spec=_spec(),
        backend=backend,
    )
    reference = await workspace.offload_tool_result("web_research", {"n": 1})
    assert reference.startswith("workspace://ws-1/workspace/output/offload/")
    stored = next(iter(hooks["written"].values()))
    assert b'"tool": "web_research"' in stored
    recalled = await workspace.recall(reference)
    assert recalled["payload"]["result"] == {"n": 1}
    assert recalled["payload"]["tool"] == "web_research"
    with pytest.raises(ValueError):
        await workspace.recall("workspace://other/x")


async def test_native_offloader_keyword_protocol():
    from agentscope.message import ToolResultBlock, UserMsg

    backend, _ = _backend_hooks()
    workspace = ControllerWorkspace(workspace_id="ws-native", controller=FakeController(), spec=_spec(), backend=backend)
    message = UserMsg("user", "保留证据与需求")
    reference = await workspace.offload_context(session_id="session", msgs=[message])
    stored = (await workspace.recall(reference))["payload"]
    assert stored["session_id"] == "session"
    assert stored["messages"][0]["id"] == message.id
    result = ToolResultBlock(id="call", name="search", output="evidence")
    reference = await workspace.offload_tool_result(session_id="session", tool_result=result)
    stored = (await workspace.recall(reference))["payload"]
    assert stored["result"]["id"] == "call"
    assert stored["session_id"] == "session"


async def test_native_offload_pages_are_session_scoped():
    import json
    from agentscope.message import UserMsg

    backend, _ = _backend_hooks()
    workspace = ControllerWorkspace(
        workspace_id="ws-reader", controller=FakeController(), spec=_spec(), backend=backend,
    )
    message = UserMsg("user", "可回读的证据" * 30)
    reference = await workspace.offload_context(session_id="session", msgs=[message])
    pages, offset = [], 0
    while offset is not None:
        page = await workspace.read(reference, session_id="session", offset=offset, limit=31)
        assert len(page["content"]) <= 31
        pages.append(page["content"])
        offset = page["next_offset"]
    assert json.loads("".join(pages))["payload"]["messages"][0] == message.model_dump(mode="json")
    with pytest.raises(PermissionError):
        await workspace.read(reference, session_id="another-session")
    with pytest.raises(ValueError):
        await workspace.read("workspace://ws-reader/workspace/output/offload/../secret.json", session_id="session")
    with pytest.raises(ValueError):
        await workspace.read("workspace://other/workspace/output/offload/a.json", session_id="session")


async def test_no_host_tool_bypass():
    """Backend 三原语只经受治理分发；缺失即拒绝，无宿主回退。"""
    bare = ControllerBackend()
    with pytest.raises(RuntimeError, match="no governed exec"):
        await bare.exec_shell(["ls"])
    with pytest.raises(RuntimeError, match="no governed read"):
        await bare.read_file("/etc/passwd")
    with pytest.raises(RuntimeError, match="no governed write"):
        await bare.write_file("/tmp/x", b"x")

    backend, hooks = _backend_hooks()
    result = await backend.exec_shell(["python", "-c", "1"], cwd="/workspace/work", timeout=5.0)
    assert result.exit_code == 0
    assert hooks["executed"] == [(["python", "-c", "1"], "/workspace/work", 5.0)]


def test_workspace_modules_do_not_import_docker_or_subprocess():
    """架构守卫：工作区适配层不直接触碰 Docker 或宿主进程。"""
    import open_deep_research.agentscope_runtime.sandbox_workspace as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    roots = {name.split(".")[0] for name in imported}
    assert "docker" not in roots
    assert "subprocess" not in roots
    assert "os" not in roots


# --------------------------------------------------------- T032 能力令牌


async def test_capability_token_roundtrip_and_expiry():
    issuer = CapabilityTokenIssuer.from_root_key(ROOT_KEY)
    token, _claims = issuer.issue(
        run_id="run-1",
        task_id="task-1",
        fence_token=2,
        profile_id="research",
        policy_digest="digest-1",
        ttl_seconds=60.0,
    )
    verified = issuer.verify(token)
    assert (verified.run_id, verified.task_id, verified.fence_token) == (
        "run-1", "task-1", 2,
    )
    # 错误密钥域不可验证。
    other = CapabilityTokenIssuer.from_root_key(
        base64.b64encode(b"another-root-key-32-bytes-long!!").decode()
    )
    with pytest.raises((ValueError, RuntimeError)):
        other.verify(token)
    # 过期令牌被拒（真实短 TTL 到期）。
    import asyncio as _asyncio

    short_token, _short_claims = issuer.issue(
        run_id="run-1", task_id="task-1", fence_token=2,
        profile_id="research", policy_digest="digest-1", ttl_seconds=0.2,
    )
    await _asyncio.sleep(0.4)
    with pytest.raises((ValueError, RuntimeError)):
        issuer.verify(short_token)


# ------------------------------------------------------- T032 出网模式


async def test_egress_mode_bridge_narrowest_and_capped(tmp_path):
    store = RunEgressModeStore("run-1", runs_dir=str(tmp_path))
    # 基线 manual：运行时请求 open 被基线封顶（fail-closed）。
    bridge = EgressModeBridge(baseline="manual", mode_store=store)
    store.set(mode="open", actor="user", fence_token=3)
    effective = bridge.effective(fence_token=3)
    assert effective.mode == "manual"
    assert effective.capped is True and effective.requested == "open"
    # 基线 open：运行时收窄立即生效。
    bridge = EgressModeBridge(baseline="open", mode_store=store)
    store.set(mode="manual", actor="user", fence_token=3)
    assert bridge.effective(fence_token=3).mode == "manual"
    # 执行租约更换不能放宽用户为本次研究选择的模式。
    assert bridge.effective(fence_token=9).mode == "manual"


# ------------------------------------------------- T032 目标判定权威


def _policy(**overrides) -> NetworkPolicy:
    # unknown_target 即出网模式基线：ask=manual / auto / allow=open / deny。
    values = {
        "mode": "allowlist",
        "allow_domains": ["docs.example"],
        "deny_domains": ["evil.example"],
        "unknown_target": "ask",
    }
    values.update(overrides)
    return NetworkPolicy(**values)


def _authority(policy, *, baseline="manual", classifier=None, approvals=None, tmp_path=None):
    approvals = approvals or SecurityApprovalStore(
        "run-1", runs_dir=str(tmp_path or ".runs-tmp")
    )
    bridge = EgressModeBridge(baseline=baseline)
    return EgressAuthority(
        policy=policy, mode_bridge=bridge, approvals=approvals, classifier=classifier
    )


async def test_authority_policy_and_modes(tmp_path):
    authority = _authority(_policy(), tmp_path=tmp_path)
    allow = await authority.decide(
        "run-1", host="docs.example", port=443, fence_token=1
    )
    assert allow.decision == "allow" and allow.source == "policy"
    denied = await authority.decide(
        "run-1", host="evil.example", port=443, fence_token=1
    )
    assert denied.decision == "deny" and denied.source == "policy"
    # 未知目标：manual → ask（人工审批保持权威）。
    unknown = await authority.decide(
        "run-1", host="unknown.example", port=443, fence_token=1
    )
    assert unknown.decision == "ask" and unknown.source == "mode"
    # open 基线（unknown_target=allow）：策略层直接放行未知目标。
    open_authority = _authority(
        _policy(unknown_target="allow", allow_domains=[]),
        baseline="open",
        tmp_path=tmp_path,
    )
    assert (
        await open_authority.decide("run-1", host="unknown.example", port=443, fence_token=1)
    ).decision == "allow"
    # deny 基线：受限目标拒绝。
    deny_authority = _authority(
        _policy(allow_domains=[], unknown_target="deny"),
        baseline="deny",
        tmp_path=tmp_path,
    )
    assert (
        await deny_authority.decide("run-1", host="whatever.example", port=443, fence_token=1)
    ).decision == "deny"


async def test_authority_auto_mode_uses_classifier_with_cache(tmp_path):
    calls = []

    class CountingInvoker:
        async def __call__(self, call):
            calls.append(call.logical_operation_id)
            from open_deep_research.sandbox.egress_classifier import EgressModelReply

            if call.structured_schema:
                return EgressModelReply(
                    status="completed",
                    structured={
                        "verdict": "allow",
                        "category": "docs",
                        "risk_tags": [],
                        "reason": "official docs domain",
                    },
                    served_model="fixture",
                )
            return EgressModelReply(status="completed", content="allow", served_model="fixture")

    classifier = EgressClassifier(limits=EgressClassifierLimits(stages="both"))
    authority = _authority(
        _policy(allow_domains=[], unknown_target="auto"),
        baseline="auto",
        classifier=classifier,
        tmp_path=tmp_path,
    )
    authority.bind_classifier_invoker(CountingInvoker())
    first = await authority.decide(
        "run-1",
        host="unknown.example",
        port=443,
        capability="tool.egress",
        intent="research docs",
        fence_token=1,
    )
    assert first.decision == "allow" and first.source == "classifier"
    second = await authority.decide(
        "run-1",
        host="unknown.example",
        port=443,
        capability="tool.egress",
        intent="research docs",
        fence_token=1,
    )
    assert second.decision == "allow"
    assert len(calls) == 1  # 第二次命中分类缓存

    # 非 tool.egress 能力不进分类器，保持 ask。
    other = await authority.decide(
        "run-1", host="unknown.example", port=443, capability="model.call", fence_token=1
    )
    assert other.decision == "ask"


async def test_human_decisions_and_version_conflict(tmp_path):
    approvals = SecurityApprovalStore("run-1", runs_dir=str(tmp_path))
    state = approvals.check_target(
        "tool.egress", {"host": "trusted.example", "port": 443}, 5
    )
    target_id, version = state["target_id"], state["version"]
    # 版本冲突：expected_version 不匹配 → 拒绝变更。
    with pytest.raises(ValueError, match="egress_version_conflict"):
        approvals.decide_target(
            target_id,
            decision="allow_run",
            reason="approved",
            actor="user",
            expected_version=version + 3,
            fence_token=5,
        )
    # 正确版本 → allow_run，authority 返回 human allow。
    approvals.decide_target(
        target_id,
        decision="allow_run",
        reason="approved",
        actor="user",
        expected_version=version,
        fence_token=5,
    )
    authority = _authority(
        _policy(allow_domains=[]),
        baseline="manual",
        approvals=approvals,
        tmp_path=tmp_path,
    )
    decision = await authority.decide(
        "run-1", host="trusted.example", port=443, capability="tool.egress", fence_token=5
    )
    assert decision.decision == "allow" and decision.source == "human"
    # block_run 覆盖 → 拒绝。
    blocked = approvals.check_target(
        "tool.egress", {"host": "trusted.example", "port": 443}, 5
    )
    approvals.decide_target(
        blocked["target_id"],
        decision="block_run",
        reason="revoked",
        actor="user",
        expected_version=blocked["version"],
        fence_token=5,
    )
    decision = await authority.decide(
        "run-1", host="trusted.example", port=443, capability="tool.egress", fence_token=5
    )
    assert decision.decision == "deny" and decision.source == "human"


# --------------------------------------------------- T032 密钥注入范围


async def test_worker_secret_scope_is_not_widened():
    def payload(runtime_config):
        return SandboxTaskPayloadV1(
            task_id="task-1",
            run_id="run-1",
            research_topic="t",
            researcher_state={"research_topic": "t"},
            runtime_config=runtime_config,
            profile_id="research",
            policy_digest="digest-1",
            fence_token=1,
        )

    clean = payload({"fetch_top_k": 3})
    assert_worker_secret_scope(clean)  # 干净载荷通过
    # wire 层直接拒绝凭据形状键（签发前 fail-closed）。
    with pytest.raises(ValueError, match="credential-shaped"):
        payload({"OPENAI_API_KEY": "sk-live"})
    with pytest.raises(ValueError, match="credential-shaped"):
        payload({"tavily_secret_key": "x"})
