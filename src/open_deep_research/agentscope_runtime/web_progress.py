"""Safe web progress over the existing signed task-activity channel."""

from contextvars import ContextVar

from open_deep_research.events.task_activity import publish_task_activity
from open_deep_research.web.sources import stable_id

model_search_progress = ContextVar("model_search_progress", default=None)
shadow_model_usage = ContextVar("shadow_model_usage", default=None)


def record_shadow_model(outcome):
    """Count every measured Gateway attempt, including structured-output repairs."""
    usage = shadow_model_usage.get()
    if usage is None or (outcome.error_code or "").startswith("budget_exhausted:"):
        return
    usage["model_calls"] += 1
    usage["input_tokens"] += outcome.usage.get("input_tokens", 0)
    usage["output_tokens"] += outcome.usage.get("output_tokens", 0)
    cost = outcome.response_cost_usd
    usage["cost_usd"] = (
        usage["cost_usd"] + cost
        if cost is not None and usage["cost_usd"] is not None
        else None
    )


_TITLES = {
    "query_started": "开始搜索",
    "query_completed": "收到搜索结果",
    "provider_failed": "搜索渠道暂不可用",
    "server_query": "服务端搜索查询",
    "server_searching": "服务端正在搜索",
    "server_results": "收到服务端搜索结果",
    "fetch_started": "开始读取网页",
    "fetch_backend": "切换网页读取方式",
    "fetch_completed": "网页读取完成",
    "inspection_updated": "资料检查进度",
    "shadow_completed": "旁路评估完成",
    "shadow_skipped": "旁路评估已跳过",
}


class WebProgress:
    """Bind stable operation identity and live authority to every web event."""

    def __init__(
        self,
        config,
        *,
        task_id,
        tool_call_id,
        operation_id,
        fence_token=None,
        tool_name="web_research",
    ):
        metadata = {**config.get("metadata", {}), "task_id": task_id}
        if fence_token is not None:
            metadata["run_fence_token"] = fence_token
        self.config = {**config, "metadata": metadata}
        self.task_id, self.tool_call_id = task_id, tool_call_id
        self.operation_id, self.tool_name = operation_id, tool_name

    async def __call__(self, phase, **payload):
        """Publish only public progress; content, credentials and prompts stay private."""
        identity = ":".join(
            str(payload.get(k, ""))
            for k in ("provider", "query_index", "query", "url", "backend")
        )
        title = _TITLES.get(phase, "网页研究进度")
        summary = str(
            payload.get("query")
            or payload.get("error_code")
            or payload.get("reason")
            or title
        )
        await publish_task_activity(
            self.config,
            "tool.progress",
            task_id=self.task_id,
            kind="tool",
            phase="tool_execution",
            status="warning"
            if phase in {"provider_failed", "shadow_skipped"}
            else "running",
            title=title,
            summary=summary[:500],
            payload={
                "tool_call_id": self.tool_call_id,
                "tool_name": self.tool_name,
                "web_phase": phase,
                **payload,
            },
            dedupe_key=stable_id("web", f"{self.operation_id}:{phase}:{identity}"),
        )
