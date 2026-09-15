"""原生消息的有限上下文裁剪；领域层显式指定必须保留的消息。"""

from agentscope.message import ToolCallBlock, ToolResultBlock
from open_deep_research.as_runtime.messages import validate_tool_pairs


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
