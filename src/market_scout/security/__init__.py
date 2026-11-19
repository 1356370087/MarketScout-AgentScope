"""Security boundaries for untrusted model context and runtime inputs."""

from .content import (
    ExternalEvidence,
    TrustLevel,
    inspect_untrusted_content,
    protect_tool_output,
    sanitize_report_markdown,
)

# inputs.py 依赖 langchain_core 消息类型；原生 AgentScope 运行时只使用本包的
# 纯净安全原语（content/redaction），旧消息校验入口在此环境不可用。旧运行
# 环境始终安装 langchain，此保护不会改变其导出面。
try:  # pragma: no cover - depends on environment
    from .inputs import (
        validate_client_messages,
        validate_http_configurable,
        validate_http_metadata,
    )
except ImportError:  # langchain_core absent in the native runtime environment
    validate_client_messages = validate_http_configurable = None
    validate_http_metadata = None

__all__ = [
    "ExternalEvidence",
    "TrustLevel",
    "inspect_untrusted_content",
    "protect_tool_output",
    "sanitize_report_markdown",
    "validate_client_messages",
    "validate_http_configurable",
    "validate_http_metadata",
]
