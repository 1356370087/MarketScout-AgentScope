"""AgentScope 沙箱 Workspace 适配（T031）。

把 AgentScope 的 ``WorkspaceBase``/``BackendBase``/``WorkspaceManagerBase``
三层桥接到现有 InsightForge 沙箱控制器（V7 可信控制器持有 Docker
Socket），本模块自身不导入 docker、不执行宿主机命令：

- ``ControllerWorkspaceManager``：per-agent 隔离分配 workspace id，统一
  close/close_all 生命周期；
- ``ControllerWorkspace``：initialize 经控制器 create/start 建立受资源限
  额约束的任务容器，close 走 collect_archive 工件回收 + stop 清理；路径
  根为容器内 ``/workspace/work``；
- ``ControllerBackend``：exec/read/write 只经应用注入的受治理分发（与
  T024/T025 的 dispatcher 同一边界），缺失即拒绝——不存在宿主机回退；
- Offloader：把上下文/工具结果/数据块外置到容器输出卷并返回可回查引用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentscope.app.workspace_manager._base import (
    IsolationPolicy,
    WorkspaceManagerBase,
)
from agentscope.tool._builtin._backend import BackendBase, ExecResult
from agentscope.workspace import WorkspaceBase

from open_deep_research.agentscope_runtime.mcp import NativeMcpServer
from open_deep_research.sandbox.schema import runtime_digest
from open_deep_research.sandbox.wire import SandboxTaskPayloadV1

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = "/workspace/work"
OFFLOAD_ROOT = "/workspace/output/offload"

ExecDispatcher = Callable[
    [list[str], str | None, float | None], Awaitable[ExecResult]
]
FileRead = Callable[[str], Awaitable[bytes]]
FileWrite = Callable[[str, bytes], Awaitable[None]]


class ControllerBackend(BackendBase):
    """经受治理分发的容器执行后端；无宿主机回退。

    ``exec/read/write`` 三个原语全部委托给应用注入的沙箱区执行器（与原生
    Toolkit 的 dispatcher 同一信任边界）。任何原语缺失时拒绝执行而不是
    退回本机。
    """

    def __init__(
        self,
        *,
        exec_dispatch: ExecDispatcher | None = None,
        read: FileRead | None = None,
        write: FileWrite | None = None,
    ) -> None:
        self._exec_dispatch = exec_dispatch
        self._read = read
        self._write = write

    def _require(self, hook: Any, name: str):
        if hook is None:
            raise RuntimeError(
                f"sandbox workspace backend has no governed {name} dispatcher; "
                "host fallback is not permitted"
            )
        return hook

    async def exec_shell(
        self, command: list[str], *, cwd: str | None = None, timeout: float | None = None
    ) -> ExecResult:
        dispatch = self._require(self._exec_dispatch, "exec")
        return await dispatch(list(command), cwd, timeout)

    async def read_file(self, path: str) -> bytes:
        read = self._require(self._read, "read")
        return await read(path)

    async def write_file(self, path: str, data: bytes) -> None:
        write = self._require(self._write, "write")
        await write(path, data)


@dataclass(frozen=True, slots=True)
class ControllerWorkspaceSpec:
    """控制器任务与资源限额的绑定描述。"""

    run_id: str
    task_id: str
    fence_token: int
    profile: Any
    profile_id: str
    policy_digest: str
    payload: SandboxTaskPayloadV1 | None = None


class ControllerWorkspace(WorkspaceBase):
    """由现有控制器任务支撑的受治理工作区。"""

    def __init__(
        self,
        *,
        workspace_id: str,
        controller: Any,
        spec: ControllerWorkspaceSpec,
        backend: ControllerBackend,
        artifact_dir: Path | None = None,
    ) -> None:
        super().__init__(workspace_id=workspace_id)
        self.controller = controller
        self.spec = spec
        self.backend = backend
        self.artifact_dir = artifact_dir
        self.container_id: str | None = None
        self.workdir = WORKSPACE_ROOT
        self._mcps: list[NativeMcpServer] = []

    def _payload(self) -> SandboxTaskPayloadV1:
        if self.spec.payload is not None:
            return self.spec.payload
        return SandboxTaskPayloadV1(
            task_id=self.spec.task_id,
            run_id=self.spec.run_id,
            research_topic="workspace",
            researcher_state={"research_topic": "workspace"},
            runtime_config={},
            profile_id=self.spec.profile_id,
            policy_digest=self.spec.policy_digest,
            fence_token=self.spec.fence_token,
            wave_id="workspace",
        )

    async def initialize(self) -> None:
        """经控制器 create/start 建立任务容器（资源限额由 profile 准入）。"""
        from open_deep_research.agentscope_runtime.sandbox_policy import CapabilityTokenIssuer

        issuer = CapabilityTokenIssuer.for_controller(self.controller)
        token, _claims = issuer.issue(
            run_id=self.spec.run_id,
            task_id=self.spec.task_id,
            fence_token=self.spec.fence_token,
            profile_id=self.spec.profile_id,
            policy_digest=self.spec.policy_digest,
            ttl_seconds=3600.0,
        )
        created = await self.controller.create_task(
            payload=self._payload(),
            task_token=token,
            runtime_digest_value=runtime_digest(self.spec.profile),
        )
        self.container_id = created.container_id
        try:
            await self.controller.start_task(self.container_id)
        except BaseException:
            await self._stop_container(preserve_error=True)
            raise
        self.is_alive = True

    async def _stop_container(self, *, preserve_error: bool = False) -> None:
        container_id = self.container_id
        if container_id is None:
            return
        try:
            await self.controller.stop_task(container_id)
        except BaseException as exc:
            if not preserve_error:
                raise
            logger.warning("workspace container cleanup failed: %s", type(exc).__name__)
        else:
            self.container_id = None
            self.is_alive = False

    async def close(self) -> None:
        """收集工件后停止任务容器；异常时保留清理入口。"""
        failed = False
        try:
            for server in self._mcps:
                await server.close()
            self._mcps.clear()
            if self.container_id is None:
                return
            try:
                archive = await self.controller.collect_archive(self.container_id)
            except Exception as exc:  # noqa: BLE001 - 清理路径不因收集失败而中止
                logger.warning("workspace archive collection failed: %s", exc)
                archive = None
            if archive and self.artifact_dir is not None:
                self.artifact_dir.mkdir(parents=True, exist_ok=True)
                target = self.artifact_dir / f"{self.workspace_id}.tar"
                target.write_bytes(archive)
        except BaseException:
            failed = True
            raise
        finally:
            await self._stop_container(preserve_error=failed)

    async def get_instructions(self) -> str:
        resources = getattr(self.spec.profile, "resources", None)
        limits = resources.model_dump(mode="json") if resources is not None else {}
        return (
            "Sandboxed workspace. All file and shell operations execute inside "
            f"the governed controller task; the workdir root is {WORKSPACE_ROOT}. "
            f"Resource limits: {json.dumps(limits, sort_keys=True)}."
        )

    async def add_mcp(
        self,
        mcp_client: Any,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """登记原生 MCP 连接；随工作区关闭统一释放。"""
        if isinstance(mcp_client, NativeMcpServer):
            self._mcps.append(mcp_client)
            await mcp_client.ensure()

    async def remove_mcp(
        self,
        name: str,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        remaining = []
        for server in self._mcps:
            if server.client.name == name:
                await server.close()
            else:
                remaining.append(server)
        self._mcps = remaining

    # ------------------------------------------------- Offloader 协议实现

    async def _offload(self, kind: str, payload: Any) -> str:
        reference = (
            f"{OFFLOAD_ROOT}/{kind}-{uuid.uuid4().hex}.json"
        )
        body = json.dumps(
            {"kind": kind, "payload": payload}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        await self.backend.write_file(reference, body)
        return f"workspace://{self.workspace_id}{reference}"

    async def offload_context(self, session_id: Any, msgs: Any = None) -> str:
        """Implement native Offloader while retaining the earlier one-argument API."""
        payload = session_id if msgs is None else {
            "session_id": session_id,
            "messages": [message.model_dump(mode="json") for message in msgs],
        }
        return await self._offload("context", payload)

    async def offload_tool_result(self, session_id: str, tool_result: Any) -> str:
        from agentscope.message import ToolResultBlock

        if isinstance(tool_result, ToolResultBlock):
            return await self._offload("tool_result", {
                "session_id": session_id, "result": tool_result.model_dump(mode="json"),
            })
        return await self._offload("tool_result", {"tool": session_id, "result": tool_result})

    async def offload_data_block(self, block: Any) -> Any:
        import base64
        from agentscope.message import Base64Source, DataBlock, URLSource

        if isinstance(block, DataBlock):
            if not isinstance(block.source, Base64Source):
                return block
            body = base64.b64decode(block.source.data, validate=True)
            path = f"{OFFLOAD_ROOT}/data-{uuid.uuid4().hex}"
            await self.backend.write_file(path, body)
            copied = block.model_copy(deep=True)
            copied.source = URLSource(
                url=f"workspace://{self.workspace_id}{path}",
                media_type=block.source.media_type,
            )
            return copied
        digest = hashlib.sha256(str(block).encode("utf-8")).hexdigest()[:16]
        return await self._offload(f"block-{digest}", block)

    async def recall(self, reference: str) -> Any:
        """按外置引用回查内容（Offloader 的逆向读取）。"""
        if not reference.startswith(f"workspace://{self.workspace_id}"):
            raise ValueError("offload reference does not belong to this workspace")
        path = reference[len(f"workspace://{self.workspace_id}"):]
        body = await self.backend.read_file(path)
        return json.loads(body.decode("utf-8"))

    async def read(self, reference, *, session_id, offset=0, limit=4096):
        """Read a session-owned offload page, never arbitrary workspace files."""
        from pathlib import PurePosixPath
        from urllib.parse import urlsplit

        if offset < 0 or not 1 <= limit <= 8192:
            raise ValueError("invalid context artifact page")
        parsed = urlsplit(reference)
        path = PurePosixPath(parsed.path)
        if (
            parsed.scheme != "workspace"
            or parsed.netloc != str(self.workspace_id)
            or parsed.query or parsed.fragment
            or str(path.parent) != OFFLOAD_ROOT
            or path.suffix != ".json"
        ):
            raise ValueError("context artifact escapes offload directory")
        value = await self.recall(reference)
        if value.get("payload", {}).get("session_id") != str(session_id):
            raise PermissionError("context artifact belongs to another session")
        content = json.dumps(value, ensure_ascii=False)
        end = min(len(content), offset + limit)
        return {
            "reference": reference,
            "content": content[offset:end],
            "next_offset": end if end < len(content) else None,
        }


class ControllerWorkspaceManager(WorkspaceManagerBase):
    """原生 workspace 管理器：per-agent 分配，控制器任务支撑。"""

    def __init__(
        self,
        *,
        controller_factory: Callable[[], Any],
        spec_builder: Callable[[str, str, str], ControllerWorkspaceSpec],
        backend_factory: Callable[[str], ControllerBackend],
        artifact_root: Path | None = None,
        isolation: IsolationPolicy = IsolationPolicy.PER_AGENT,
    ) -> None:
        super().__init__(isolation=isolation)
        self._controller_factory = controller_factory
        self._spec_builder = spec_builder
        self._backend_factory = backend_factory
        self._artifact_root = artifact_root
        self._workspaces: dict[str, ControllerWorkspace] = {}

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
    ) -> ControllerWorkspace:
        if workspace_id is None:
            workspace_id = await self.assign_workspace_id(
                user_id=user_id, agent_id=agent_id, session_id=session_id
            )
        existing = self._workspaces.get(workspace_id)
        if existing is not None:
            if existing.is_alive:
                return existing
            await self.close(workspace_id)
        workspace = ControllerWorkspace(
            workspace_id=workspace_id,
            controller=self._controller_factory(),
            spec=self._spec_builder(user_id, agent_id, workspace_id),
            backend=self._backend_factory(workspace_id),
            artifact_dir=(
                self._artifact_root / workspace_id if self._artifact_root else None
            ),
        )
        try:
            await workspace.initialize()
        except BaseException:
            if workspace.container_id is not None:
                self._workspaces[workspace_id] = workspace
            raise
        self._workspaces[workspace_id] = workspace
        return workspace

    async def close(self, workspace_id: str) -> None:
        workspace = self._workspaces.get(workspace_id)
        if workspace is not None:
            await workspace.close()
            self._workspaces.pop(workspace_id, None)

    async def close_all(self) -> None:
        ids = list(self._workspaces)
        for workspace_id in ids:
            await self.close(workspace_id)


__all__ = [
    "OFFLOAD_ROOT",
    "WORKSPACE_ROOT",
    "ControllerBackend",
    "ControllerWorkspace",
    "ControllerWorkspaceManager",
    "ControllerWorkspaceSpec",
]
