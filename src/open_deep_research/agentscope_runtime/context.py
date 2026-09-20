"""原生消息的有限上下文裁剪；领域层显式指定必须保留的消息。"""

import json
from pathlib import Path
from urllib.parse import urlsplit

from agentscope.message import ToolCallBlock, ToolResultBlock
from open_deep_research.agentscope_runtime.messages import validate_tool_pairs
from agentscope.middleware import MiddlewareBase
from uuid import uuid4


class RunContextOffloader:
    """Persist trusted orchestration context beside the run's recovery artifacts.

    This is a host control-plane artifact writer, not a sandbox file-tool fallback.
    The factory supplies an authorized run lease; stale owners cannot write.
    """

    def __init__(self, runs_dir, recovery):
        # Keep model-readable payloads separate from manifests and credentials.
        self.directory = Path(runs_dir) / recovery.lease.run_id / "context" / "offloaded"
        self.recovery = recovery

    async def _write(self, payload):
        name = f"{uuid4().hex}.json"
        body = json.dumps(payload, ensure_ascii=False)
        async with self.recovery.store.transaction(self.recovery.lease):
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / name).write_text(body, encoding="utf-8")
        return f"run-context://{self.recovery.lease.run_id}/{name}"

    async def offload_context(self, session_id, msgs, **kwargs):
        """Persist the messages selected by AgentScope before replacement."""
        return await self._write({
            "session_id": str(session_id),
            "messages": [message.model_dump(mode="json") for message in msgs],
        })

    async def offload_tool_result(self, session_id, tool_result, **kwargs):
        """Persist the complete block handed off by native tool truncation."""
        return await self._write({
            "session_id": str(session_id),
            "tool_result": tool_result.model_dump(mode="json"),
        })

    async def read(self, reference, *, session_id, offset=0, limit=4096):
        """Read a bounded character page using the caller's trusted session ID.

        The model supplies only the reference and pagination. The caller binds
        session_id from the active agent, never from the tool's input.
        """
        if offset < 0 or not 1 <= limit <= 8192:
            raise ValueError("invalid context artifact page")
        parsed = urlsplit(reference)
        if (
            parsed.scheme != "run-context"
            or parsed.netloc != self.recovery.lease.run_id
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise ValueError("context artifact does not belong to this run")
        directory = self.directory.resolve()
        path = (directory / parsed.path[1:]).resolve()
        if path.parent != directory or path.suffix != ".json":
            raise ValueError("context artifact escapes offload directory")
        async with self.recovery.store.transaction(self.recovery.lease):
            content = path.read_text(encoding="utf-8")
            payload = json.loads(content)
            if payload["session_id"] != str(session_id):
                raise PermissionError("context artifact belongs to another session")
        end = min(len(content), offset + limit)
        return {
            "reference": reference,
            "content": content[offset:end],
            "next_offset": end if end < len(content) else None,
        }


class NativeContextCompactor:
    """保留系统指令、首个问题、最新交互及领域保护消息的完整工具组。

    protected_ids 由调用者从简报、覆盖契约、证据和反馈中确定；不猜测文本语义。
    字符预算只控制传输上下文大小，不冒充提供商的精确 token 计数。
    """

    def __init__(self, *, max_chars: int, protected_ids: set[str]):
        if max_chars < 1:
            raise ValueError("context character budget must be positive")
        self.max_chars = max_chars
        self.protected_ids = frozenset(protected_ids)

    async def __call__(self, messages):
        validate_tool_pairs(messages, complete=False)
        groups, group, pending = [], [], set()
        for message in messages:
            group.append(message)
            for block in message.content:
                if isinstance(block, ToolCallBlock):
                    pending.add(block.id)
                elif isinstance(block, ToolResultBlock):
                    pending.discard(block.id)
            if not pending:
                groups.append(group)
                group = []
        if group:
            groups.append(group)
        first_user = next((m.id for m in messages if m.role == "user"), None)
        protected = {
            i
            for i, items in enumerate(groups)
            if any(
                m.role == "system" or m.id == first_user or m.id in self.protected_ids
                for m in items
            )
        }
        if groups:
            protected.add(len(groups) - 1)
        selected = set(protected)
        sizes = [sum(len(m.model_dump_json()) for m in items) for items in groups]
        used = sum(sizes[i] for i in selected)
        if used > self.max_chars:
            raise ValueError("protected context exceeds character budget")
        for i in reversed(range(len(groups))):
            if i not in selected and used + sizes[i] <= self.max_chars:
                selected.add(i)
                used += sizes[i]
        result = [
            m.model_copy(deep=True)
            for i, items in enumerate(groups)
            if i in selected
            for m in items
        ]
        validate_tool_pairs(result, complete=False)
        return result


class ResearchContextMiddleware(MiddlewareBase):
    """Bound native research context while preserving domain messages and pairs."""

    def __init__(self, *, max_chars: int, offloader=None):
        self.max_chars = max_chars
        self.offloader = offloader

    async def on_compress_context(self, agent, input_kwargs, next_handler):
        messages = agent.state.context
        if sum(len(m.model_dump_json()) for m in messages) <= self.max_chars:
            return
        protected = {m.id for m in messages if m.metadata.get("research_protected")}
        # Native Agent coalesces successive tool rounds into one assistant Msg.
        # Split that envelope before selecting complete call/result groups.
        candidates = []
        for message in messages:
            if message.role == "assistant" and len(message.content) > 1 and message.id not in protected:
                candidates.extend(message.model_copy(deep=True, update={"id": uuid4().hex, "content": [block.model_copy(deep=True)], "usage": None}) for block in message.content)
            else:
                candidates.append(message)
        compact = NativeContextCompactor(max_chars=self.max_chars, protected_ids=protected)
        kept = await compact(candidates)
        if sum(len(m.model_dump_json()) for m in kept) >= sum(len(m.model_dump_json()) for m in messages):
            raise ValueError("research context cannot shrink within its protected budget")
        if self.offloader is None:
            raise ValueError("research context requires an authorized offloader")
        reference = await self.offloader.offload_context(agent.state.session_id, messages)
        # Commit the replacement only after externalization has succeeded.
        agent.state.context = kept
        agent.state.middle_context.setdefault("research_context_refs", []).append(reference)
