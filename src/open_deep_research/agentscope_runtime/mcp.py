"""AgentScope 原生 MCP 连接器（T026/T027/T028）。

基于 ``agentscope.mcp.MCPClient``（mcp 1.30 SDK）替换依赖 mcp 2.x 与
LangChain 的旧 ``tools/mcp`` 适配层：

- 传输映射：stdio（有状态长连接）/ streamable_http（逐调用临时会话，保持旧
  session-per-call 语义）/ sse（框架按 URL 路径窄判定，路径不符即拒绝）。
- 工具转换：远端 ``inputSchema`` 编译为带完整 JSON Schema 校验器的 Pydantic
  模型（嵌套约束不丢失），并通过 ``model_definition`` 把原始 schema 原样投影
  给模型绑定。
- OAuth：RFC 8693 token exchange、按 owner 的令牌缓存（网关 vault / 内存
  token store 双通道）、v2 ``URL_ELICITATION_REQUIRED`` 与旧版 ``-32003``
  错误码翻译为带受验 URL 的 ``MCPInteractionRequired``。
- 装载：与旧 loader 相同的信任边界——工具 allowlist、副作用声明、注入形
  描述阻断、HTTP surface 服务器白名单与 stdio 默认阻断。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import warnings
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import httpx
from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig
from agentscope.message import ToolResultState
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from jsonschema.validators import validator_for
from mcp import McpError
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import URL_ELICITATION_REQUIRED
from mcp.types import Tool as McpToolDescriptor
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from pydantic_core import PydanticCustomError
from referencing import Registry
from referencing.exceptions import Unresolvable

from open_deep_research.configuration import BrowserMCPConfig, Configuration
from open_deep_research.sandbox.egress_context import authorize_url, egress_authorizer
from open_deep_research.sandbox.policy import egress_target_from_url
from open_deep_research.sandbox.schema import (
    network_target_decision,
    resolve_profile,
    tool_policy_decision,
)
from open_deep_research.security.content import inspect_untrusted_content
from open_deep_research.security.network import (
    PublicWebResolver,
    validate_http_url_syntax,
    validate_response_peer,
)
from open_deep_research.security.redaction import redact_text
from open_deep_research.skills import get_skill_researcher_context
from open_deep_research.tools.base import (
    Tool,
    ToolContext,
    ToolEffect,
    ToolExecutionZone,
    ToolOrigin,
    ToolResult,
)
from open_deep_research.tools.governance import AgentRole, filter_tools_by_permission
from open_deep_research.tools.token_store import get_token_store

logger = logging.getLogger(__name__)

_HTTP_TRANSPORTS = frozenset({"http", "streamable_http"})
_SUPPORTED_TRANSPORTS = frozenset({"stdio", "sse"}) | _HTTP_TRANSPORTS
_SSE_PATH_SUFFIXES = ("/sse", "/messages/")

_LEGACY_INTERACTION_REQUIRED = -32003
"""Pre-v2 server convention for "visit this URL to interact"; kept for older servers."""

_JSON_SCHEMA_TYPE_MAP: dict[str, type] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


class MCPInteractionRequired(Exception):
    """结构化交互请求，转发给审批层；携带受验后的交互 URL。"""

    def __init__(self, message: str, interaction_url: str | None = None) -> None:
        super().__init__(message)
        self.interaction_url = interaction_url


class NativeMcpToolError(RuntimeError):
    """远端工具返回 ``isError`` 结果；由治理层分类展示。"""


class _McpResponseStream(httpx.AsyncByteStream):
    def __init__(self, response):
        self.response = response

    async def __aiter__(self):
        async for chunk in self.response.content.iter_any():
            yield chunk

    async def aclose(self):
        self.response.close()


class _PublicMcpTransport(httpx.AsyncBaseTransport):
    """Use the shared public resolver for the exact socket dialed by MCP."""

    def __init__(self):
        self.session = None

    async def handle_async_request(self, request):
        url = str(request.url)
        validate_http_url_syntax(url)
        if egress_authorizer.get() is not None and await authorize_url(url, "tool.network") != "allow":
            raise PermissionError("mcp_network_authorization_denied")
        if self.session is None:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicWebResolver(), use_dns_cache=False),
                auto_decompress=False,
                trust_env=False,
            )
        timeout = request.extensions.get("timeout", {})
        response = await self.session.request(
            request.method, url, headers=request.headers.multi_items(),
            data=await request.aread(), allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=timeout.get("connect"), sock_read=timeout.get("read")),
        )
        try:
            validate_response_peer(response)
        except ValueError:
            response.close()
            raise
        return httpx.Response(response.status, headers=response.raw_headers, stream=_McpResponseStream(response))

    async def aclose(self):
        if self.session is not None:
            await self.session.close()


class _PublicMcpClient(MCPClient):
    """Keep AgentScope sessions and SDK origin checks with a guarded transport.

    HttpMCPConfig has no transport injection point in the pinned AgentScope
    version, so this override is confined to its HTTP session factory.
    """

    @staticmethod
    def _new_http_client(**kwargs):
        return httpx.AsyncClient(transport=_PublicMcpTransport(), trust_env=False, **kwargs)

    def _create_http_client(self):
        config = self.mcp_config
        if self._is_sse:
            return sse_client(config.url, headers=config.headers, timeout=config.timeout,
                              httpx_client_factory=self._new_http_client)
        return self._public_streamable_client()

    @asynccontextmanager
    async def _public_streamable_client(self):
        config = self.mcp_config
        async with self._new_http_client(headers=config.headers, timeout=config.timeout) as client:
            self._static_headers = httpx.Headers(client.headers)
            client.headers.update(self._runtime_headers)
            self._http_client = client
            try:
                async with streamable_http_client(config.url, http_client=client) as transport:
                    yield transport
            finally:
                self._http_client = None


def build_native_mcp_client(connection: dict[str, Any], *, name: str = "server") -> MCPClient:
    """把项目连接字典映射为原生 ``MCPClient``。

    - ``stdio``：必须有 ``command``，框架要求有状态长连接。
    - ``streamable_http``/``http``：必须有 ``url``；无状态客户端每次调用打开
      临时会话（与旧 session-per-call 语义一致）。
    - ``sse``：窄适配——框架仅按 URL 路径（``/sse`` 或 ``/messages/``）识别
      SSE 传输，路径不符时不会降级为 streamable HTTP，而是显式拒绝。
    """
    transport = str(connection.get("transport") or "streamable_http")
    if transport not in _SUPPORTED_TRANSPORTS:
        raise ValueError(
            f"Unsupported MCP transport {transport!r}; expected one of "
            f"{sorted(_SUPPORTED_TRANSPORTS)}"
        )
    if transport == "stdio":
        command = connection.get("command")
        if not command:
            raise ValueError("stdio MCP connection requires a 'command' entry")
        return MCPClient(
            name=name,
            is_stateful=True,
            mcp_config=StdioMCPConfig(
                command=str(command),
                args=[str(arg) for arg in connection.get("args") or []] or None,
                env={
                    str(key): str(value)
                    for key, value in (connection.get("env") or {}).items()
                }
                or None,
            ),
        )
    url = connection.get("url")
    if not url:
        raise ValueError(f"{transport} MCP connection requires a 'url' entry")
    headers = connection.get("headers")
    if transport == "sse":
        path = urlsplit(str(url)).path
        if not path.endswith(_SSE_PATH_SUFFIXES):
            raise ValueError(
                "SSE MCP endpoint must end with '/sse' or '/messages/'; the native "
                "client selects the SSE transport by URL path only"
            )
    client_type = _PublicMcpClient if connection.get("restricted_network") else MCPClient
    return client_type(
        name=name,
        is_stateful=False,
        mcp_config=HttpMCPConfig(
            url=str(url),
            headers=dict(headers) if headers else None,
        ),
    )


def _annotation_from_schema(schema: dict[str, Any]) -> Any:
    """Map a JSON Schema property descriptor onto a Python annotation."""
    if schema.get("enum"):
        values = tuple(schema["enum"])
        if all(isinstance(value, str) for value in values):
            return Literal[values]
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null = [item for item in schema_type if item != "null"]
        nullable = len(non_null) < len(schema_type)
        branches = [
            _JSON_SCHEMA_TYPE_MAP.get(item, Any)
            for item in non_null
            if isinstance(item, str)
        ] or [Any]
        annotation: Any = branches[0]
        for branch in branches[1:]:
            annotation = annotation | branch
        return annotation | None if nullable else annotation
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            return list[_annotation_from_schema(items)]  # type: ignore[misc]
        return list
    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        branches = [
            _annotation_from_schema(branch)
            for branch in schema["anyOf"]
            if isinstance(branch, dict)
        ] or [Any]
        merged: Any = branches[0]
        for branch in branches[1:]:
            merged = merged | branch
        return merged
    if not isinstance(schema_type, str):
        return Any
    return _JSON_SCHEMA_TYPE_MAP.get(schema_type, Any)


def build_args_schema(tool_name: str, input_schema: dict[str, Any]) -> type:
    """Compile an MCP ``inputSchema`` into a Pydantic model for governed calls.

    浅层类型映射保持工具调用人机工学，model 级校验器保留完整远端 JSON
    Schema 契约（含 maxLength/pattern/additionalProperties 等嵌套约束）。
    """
    validator_class = validator_for(input_schema)
    validator_class.check_schema(input_schema)
    # Remote schema references must not open an ungoverned HTTP connection.
    schema_validator = validator_class(input_schema, registry=Registry())

    class SchemaValidatedMCPArgs(BaseModel):
        model_config = ConfigDict(extra="allow")

        @model_validator(mode="before")
        @classmethod
        def validate_mcp_input_schema(cls, value: Any) -> Any:
            try:
                schema_validator.validate(value)
            except JSONSchemaValidationError as exc:
                path = ".".join(str(item) for item in exc.absolute_path)
                location = f" at '{path}'" if path else ""
                raise PydanticCustomError(
                    "mcp_schema_validation",
                    f"Input does not match MCP JSON Schema{location}: {exc.message}",
                ) from exc
            except Unresolvable as exc:
                raise PydanticCustomError(
                    "mcp_schema_reference",
                    "MCP JSON Schema reference cannot be resolved locally",
                ) from exc
            return value

    properties = input_schema.get("properties") if isinstance(input_schema, dict) else None
    properties = properties if isinstance(properties, dict) else {}
    required = set(
        input_schema.get("required") or [] if isinstance(input_schema, dict) else []
    )
    fields: dict[str, tuple[Any, Any]] = {}
    for name, raw_schema in properties.items():
        schema = raw_schema if isinstance(raw_schema, dict) else {}
        annotation = _annotation_from_schema(schema)
        if name in required and "default" not in schema:
            fields[str(name)] = (annotation, Field(...))
        else:
            fields[str(name)] = (annotation, schema.get("default"))
    model_name = "MCPArgs_" + re.sub(r"\W", "_", tool_name or "tool")
    return create_model(  # type: ignore[call-overload]
        model_name,
        __base__=SchemaValidatedMCPArgs,
        **fields,
    )


def _block_text(block: Any) -> str | None:
    text = getattr(block, "text", None)
    return text if isinstance(text, str) else None


def _render_blocks(blocks: Iterable[Any]) -> str:
    """文本块拼接为字符串；混合内容序列化为稳定 JSON（与旧结果转换一致）。"""
    blocks = list(blocks)
    texts = [text for text in (_block_text(block) for block in blocks) if text is not None]
    if not blocks:
        return ""
    if len(texts) == len(blocks):
        return "\n".join(texts)
    return json.dumps(
        [
            block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else str(block)
            for block in blocks
        ],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _find_mcp_error(exc: BaseException) -> McpError | None:
    if isinstance(exc, McpError):
        return exc
    for nested in getattr(exc, "exceptions", ()):
        if found := _find_mcp_error(nested):
            return found
    return None


def _interaction_required_message(code: int, error_data: Any) -> str | None:
    """Build the HITL message for an interaction-required error, if it is one."""
    data = error_data if isinstance(error_data, dict) else {}
    if code == URL_ELICITATION_REQUIRED:
        parts: list[str] = []
        for item in data.get("elicitations") or []:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "Required interaction")
            if url := item.get("url"):
                message = f"{message} {url}"
            parts.append(message)
        return "\n".join(parts) if parts else "Required interaction"
    if code == _LEGACY_INTERACTION_REQUIRED:
        message_payload = data.get("message", {})
        error_message = "Required interaction"
        if isinstance(message_payload, dict):
            error_message = message_payload.get("text") or error_message
        if url := data.get("url"):
            error_message = f"{error_message} {url}"
        return error_message
    return None


def _validated_interaction_url(value: str | None) -> str | None:
    """Accept a bounded HTTPS OAuth URL without user-info or private IP literals."""
    if not value or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        if parsed.scheme.lower() == "http" and host.lower() not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global and not address.is_loopback:
            return None
        netloc = host.lower()
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunsplit(
            (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
        )
    except (TypeError, ValueError):
        return None


def translate_mcp_interaction(exc: BaseException) -> MCPInteractionRequired | None:
    """把 MCP 交互要求错误翻译为原生异常；非交互错误返回 None。"""
    mcp_error = _find_mcp_error(exc)
    if mcp_error is None:
        return None
    error_details = mcp_error.error
    error_data = getattr(error_details, "data", None) or {}
    error_message = _interaction_required_message(
        getattr(error_details, "code", None), error_data
    )
    if error_message is None:
        return None
    match = re.search(r"https?://[^\s]+", error_message)
    interaction_url = _validated_interaction_url(
        match.group(0).rstrip(".,)") if match else None
    )
    return MCPInteractionRequired(error_message, interaction_url)


async def exchange_mcp_subject_token(
    subject_token: str,
    base_mcp_url: str,
    *,
    restricted: bool = False,
) -> dict[str, Any] | None:
    """Exchange a trusted server-side subject token for an MCP access token."""
    form_data = {
        "client_id": "mcp_default",
        "subject_token": subject_token,
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "resource": base_mcp_url.rstrip("/") + "/mcp",
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }
    try:
        token_url = base_mcp_url.rstrip("/") + "/oauth/token"
        if restricted:
            validate_http_url_syntax(token_url)
            if await authorize_url(token_url, "tool.network", True) != "allow":
                return None
        connector = aiohttp.TCPConnector(resolver=PublicWebResolver(), use_dns_cache=False) if restricted else None
        async with (
            aiohttp.ClientSession(connector=connector, trust_env=False) as session,
            session.post(
                token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=form_data,
                allow_redirects=False,
            ) as response,
        ):
            if restricted:
                validate_response_peer(response)
            if response.status == 200:
                return await response.json()
            response_body = redact_text(await response.text())[:512]
            logger.warning(
                "MCP token exchange failed status=%s response=%s",
                response.status,
                response_body,
            )
    except Exception as exc:  # noqa: BLE001 - exchange 失败按无令牌处理并脱敏告警
        logger.warning(
            "MCP token exchange error: %s",
            redact_text(str(exc))[:512],
        )
    return None


async def get_tokens(config: dict[str, Any]) -> dict[str, Any] | None:
    """Retrieve a user's cached MCP tokens when they have not expired."""
    metadata = config.get("metadata", {})
    if metadata.get("sandbox_gateway_physical"):
        vault = config.get("configurable", {}).get("_sandbox_credential_vault")
        if not isinstance(vault, dict):
            return None
        tokens = vault.get("mcp_tokens")
        if not isinstance(tokens, dict):
            return None
        expires_at = tokens.get("_expires_at")
        if expires_at is not None and float(expires_at) <= datetime.now(
            UTC
        ).timestamp():
            vault.pop("mcp_tokens", None)
            return None
        return {key: value for key, value in tokens.items() if key != "_expires_at"}
    if not config.get("configurable", {}).get("thread_id"):
        return None
    user_id = config.get("metadata", {}).get("owner")
    if not user_id:
        return None
    store = get_token_store()
    tokens = await store.get(str(user_id))
    if not tokens:
        return None
    expires_in = tokens.value.get("expires_in")
    if not expires_in:
        return tokens.value
    expiration_time = tokens.created_at + timedelta(seconds=expires_in)
    if datetime.now(UTC) > expiration_time:
        await store.delete(str(user_id))
        return None
    return tokens.value


async def set_tokens(config: dict[str, Any], tokens: dict[str, Any]) -> None:
    """Store MCP tokens in the configured per-user token store."""
    metadata = config.get("metadata", {})
    if metadata.get("sandbox_gateway_physical"):
        configurable = config.setdefault("configurable", {})
        vault = configurable.setdefault("_sandbox_credential_vault", {})
        if not isinstance(vault, dict):
            raise RuntimeError("sandbox_gateway_credential_vault_invalid")
        value = dict(tokens)
        if tokens.get("expires_in"):
            value["_expires_at"] = datetime.now(UTC).timestamp() + float(
                tokens["expires_in"]
            )
        vault["mcp_tokens"] = value
        return
    if not config.get("configurable", {}).get("thread_id"):
        return
    user_id = config.get("metadata", {}).get("owner")
    if user_id:
        await get_token_store().set(str(user_id), tokens)


async def fetch_tokens(
    config: dict[str, Any], *, role: AgentRole = AgentRole.RESEARCHER
) -> dict[str, Any] | None:
    """Return cached tokens or perform RFC 8693 exchange when configured."""
    current_tokens = await get_tokens(config)
    if current_tokens:
        return current_tokens
    subject_token = config.get("configurable", {}).get("mcp_subject_token")
    mcp_config = config.get("configurable", {}).get("mcp_config")
    if not subject_token or not mcp_config or not mcp_config.get("url"):
        return None
    restricted = _restricted_network(config)
    if restricted and (not _discovery_tool_names(config, set(mcp_config.get("tools") or []),
                                               mcp_config.get("tool_effects") or {}, ToolOrigin.MCP, role)
                       or not await _authorize_discovery_url(mcp_config["url"], config)):
        return None
    tokens = await exchange_mcp_subject_token(subject_token, mcp_config["url"], restricted=restricted)
    if tokens:
        await set_tokens(config, tokens)
    return tokens


def _description_is_safe(descriptor: McpToolDescriptor, configurable: Configuration) -> bool:
    description = str(descriptor.description or "")
    return not inspect_untrusted_content(
        description[: configurable.max_mcp_description_chars]
    )


def _constant_egress_url(url: str, args: dict[str, Any]) -> list[str]:
    del args
    return [url]


class NativeMcpServer:
    """原生 MCP 服务器的发现与调用生命周期。

    有状态（stdio）连接惰性建立、幂等复用，由 ``close``/``aclose`` 显式释放
    （装载后的工具在整段运行期间共享同一连接）；无状态（HTTP/SSE）每次调用
    使用临时会话，SDK 自行释放。取消传播由 SDK 的上下文管理器保证。
    """

    def __init__(self, connection: dict[str, Any], *, name: str) -> None:
        self.connection = dict(connection)
        self.client = build_native_mcp_client(connection, name=name)
        self._lock: Any = None
        self._closed = False

    async def _ensure_lock(self) -> Any:
        if self._lock is None:
            import asyncio

            self._lock = asyncio.Lock()
        return self._lock

    async def open(self) -> None:
        """有状态连接幂等建立；一次性生命周期，关闭后不可复活。"""
        if self._closed:
            raise RuntimeError("MCP server connection was closed")
        if not self.client.is_stateful or self.client.is_connected:
            return
        lock = await self._ensure_lock()
        async with lock:
            if not self.client.is_connected:
                await self.client.connect()

    # ``ensure`` 是工具调用路径使用的稳定名称。
    ensure = open

    async def close(self) -> None:
        self._closed = True
        if self.client.is_stateful and self.client.is_connected:
            await self.client.close()

    aclose = close

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def discover(self) -> list[McpToolDescriptor]:
        await self.open()
        return await self.client.list_raw_tools()

    def adapt(
        self,
        descriptor: McpToolDescriptor,
        *,
        origin: ToolOrigin,
        effect: ToolEffect,
        retryable: bool,
        egress_urls_url: str | None,
        auth_satisfied: bool = False,
    ) -> NativeMcpTool:
        return NativeMcpTool(
            self,
            descriptor,
            origin=origin,
            effect=effect,
            retryable=retryable,
            egress_urls_url=egress_urls_url,
            auth_satisfied=auth_satisfied,
        )


class NativeMcpTool:
    """实现项目 Tool 协议的远端 MCP 工具；原始 schema 原样投影给模型。"""

    def __init__(
        self,
        server: NativeMcpServer,
        descriptor: McpToolDescriptor,
        *,
        origin: ToolOrigin,
        effect: ToolEffect,
        retryable: bool,
        egress_urls_url: str | None,
        auth_satisfied: bool = False,
    ) -> None:
        self.server = server
        self.descriptor = descriptor
        self.name = descriptor.name
        self.input_schema = build_args_schema(
            descriptor.name, dict(descriptor.inputSchema or {})
        )
        self.origin = origin
        self.effect = effect
        self.retryable = retryable
        self.execution_zone = ToolExecutionZone.GATEWAY
        self.concurrency_safe = False
        self.supports_idempotency = False
        self.max_output_chars: int | None = None
        self.auth_satisfied = auth_satisfied
        self._egress_urls_url = egress_urls_url
        self.model_definition = {
            "name": descriptor.name,
            "description": descriptor.description or "",
            # 保留原始引用：模型绑定拿到未丢约束的远端 schema。
            "parameters": descriptor.inputSchema or {},
        }

    async def description(self, input: Any = None) -> str:
        return str(self.descriptor.description or "")

    def prompt(self, config: dict[str, Any]) -> str | None:
        return None

    def is_enabled(self, config: dict[str, Any]) -> bool:
        return True

    def egress_urls(self, input: dict[str, Any]) -> list[str]:
        urls = [self._egress_urls_url] if self._egress_urls_url else []
        if self.origin is ToolOrigin.BROWSER:
            urls.extend(input[key] for key in ("url", "target_url", "href")
                        if isinstance(input.get(key), str) and input[key])
        return list(dict.fromkeys(urls))

    async def call(
        self,
        input: Any,
        context: ToolContext,
        on_progress: Any = None,
    ) -> ToolResult[str]:
        await self.server.ensure()
        handle = await self.server.client.get_tool(self.descriptor.name)
        try:
            chunk = await handle(**input.model_dump())
        except BaseException as exc:
            interaction = translate_mcp_interaction(exc)
            if interaction is not None:
                raise interaction from exc
            raise
        if chunk.state is ToolResultState.ERROR:
            raise NativeMcpToolError(
                _render_blocks(chunk.content) or "MCP tool returned an error with no text content"
            )
        return ToolResult(output=_render_blocks(chunk.content))


def _build_browser_connection(
    browser_config: BrowserMCPConfig,
) -> dict[str, Any] | None:
    """构造浏览器 MCP 连接（与旧 ``tools/mcp/browser.py`` 同形）。"""
    if browser_config.transport == "stdio":
        if not browser_config.command:
            return None
        connection: dict[str, Any] = {
            "transport": "stdio",
            "command": browser_config.command,
            "args": list(browser_config.args or []),
        }
        if browser_config.env:
            connection["env"] = dict(browser_config.env)
        return connection
    if not browser_config.url:
        return None
    return {"transport": browser_config.transport, "url": browser_config.url}


async def _discover_via_server(
    connection: dict[str, Any],
) -> tuple[NativeMcpServer, list[McpToolDescriptor]]:
    """发现后保持连接打开；有状态服务器由工具共享、显式关闭。"""
    server = NativeMcpServer(connection, name="server")
    try:
        return server, await server.discover()
    except BaseException:
        await server.close()
        raise


async def close_native_mcp_tools(tools: Iterable[Tool]) -> None:
    """关闭工具集合引用的全部有状态 MCP 连接（每个服务器恰一次）。"""
    seen: set[int] = set()
    for tool in tools:
        server = getattr(tool, "server", None)
        if isinstance(server, NativeMcpServer) and id(server) not in seen:
            seen.add(id(server))
            await server.close()


def _restricted_network(config):
    return (Configuration.from_runnable_config(config).sandbox_enabled
            or config.get("metadata", {}).get("deployment_surface") == "http")


def _discovery_tool_names(config, names, effects, origin, role=AgentRole.RESEARCHER):
    """Authorize configured tool metadata before contacting its server."""
    declared = [NativeMcpTool(None, McpToolDescriptor(name=name, inputSchema={"type": "object"}),
                              origin=origin, effect=ToolEffect(effects[name]), retryable=False,
                              egress_urls_url=None, auth_satisfied=True)
                for name in names if name in effects]
    permitted = filter_tools_by_permission(declared, role, config)
    if _restricted_network(config):
        _, _, profile = resolve_profile(Configuration.from_runnable_config(config))
        if profile.network.mode == "offline":
            return set()
        permitted = [tool for tool in permitted if tool_policy_decision(
            profile, tool_name=tool.name, effect=tool.effect.value) != "deny"]
    # auth_satisfied here bypasses only the not-yet-fetched credential check;
    # these metadata stubs are never exposed or executed as loaded tools.
    return {tool.name for tool in permitted}


async def _authorize_discovery_url(url, config):
    if not _restricted_network(config):
        return True
    try:
        validate_http_url_syntax(url)
        target = egress_target_from_url(url)
        if target is None:
            return False
        _, _, profile = resolve_profile(Configuration.from_runnable_config(config))
        if network_target_decision(profile.network, *target) == "deny":
            return False
        if egress_authorizer.get() is not None:
            return await authorize_url(url, "tool.network", True) == "allow"
        # An HTTP/sandbox loader without the live Gateway authority cannot
        # honor approvals, revocations or run-mode overrides.
        return False
    except (ValueError, PermissionError):
        return False


async def load_native_mcp_tools(
    config: dict[str, Any],
    existing_tool_names: set[str],
    *,
    role: AgentRole = AgentRole.RESEARCHER,
) -> list[Tool]:
    """按旧 loader 的信任边界发现通用 MCP 工具（原生客户端）。"""
    configurable = Configuration.from_runnable_config(config)
    mcp_config = configurable.mcp_config
    is_http_surface = config.get("metadata", {}).get("deployment_surface") == "http"
    if not (mcp_config and mcp_config.url and mcp_config.tools):
        return []
    configured_names = set(mcp_config.tools)
    if not configured_names.issubset(mcp_config.tool_effects):
        logger.warning(
            "Blocked MCP discovery because one or more tool effects are undeclared"
        )
        return []
    if is_http_surface:
        allowed_servers = {value.rstrip("/") for value in configurable.allowed_mcp_servers}
        if mcp_config.url.rstrip("/") not in allowed_servers:
            logger.warning("Blocked non-allowlisted MCP server on HTTP surface")
            return []

    configured_names = _discovery_tool_names(config, configured_names, mcp_config.tool_effects, ToolOrigin.MCP, role)
    if not configured_names or not await _authorize_discovery_url(mcp_config.url, config):
        return []

    tokens = await fetch_tokens(config, role=role) if mcp_config.auth_required else None
    if mcp_config.auth_required and not tokens:
        return []
    headers = (
        {"Authorization": f"Bearer {tokens['access_token']}"} if tokens else None
    )
    connection = {
        "url": mcp_config.url.rstrip("/") + "/mcp",
        "headers": headers,
        "transport": "streamable_http",
        "restricted_network": _restricted_network(config),
    }
    try:
        server, available_tools = await _discover_via_server(connection)
    except Exception:  # noqa: BLE001 - 发现失败按无工具装载（fail-closed）
        return []

    loaded: list[Tool] = []
    for descriptor in available_tools:
        if descriptor.name in existing_tool_names:
            warnings.warn(
                f"MCP tool '{descriptor.name}' conflicts with existing tool name - skipping"
            )
            continue
        if descriptor.name not in configured_names:
            continue
        effect_value = mcp_config.tool_effects.get(descriptor.name)
        if effect_value is None:
            warnings.warn(
                f"MCP tool '{descriptor.name}' has no explicit tool_effects entry - skipping"
            )
            continue
        if not _description_is_safe(descriptor, configurable):
            warnings.warn(
                f"MCP tool '{descriptor.name}' has an instruction-shaped description - skipping"
            )
            continue
        effect = ToolEffect(effect_value)
        loaded.append(
            NativeMcpTool(
                server,
                descriptor,
                origin=ToolOrigin.MCP,
                effect=effect,
                retryable=effect in {ToolEffect.READ_ONLY, ToolEffect.SENSITIVE_READ},
                egress_urls_url=mcp_config.url,
                auth_satisfied=bool(mcp_config.auth_required and tokens),
            )
        )
    return loaded


async def load_native_browser_mcp_tools(
    config: dict[str, Any],
    existing_tool_names: set[str],
    *,
    role: AgentRole = AgentRole.RESEARCHER,
) -> list[Tool]:
    """按旧浏览器 loader 的信任边界发现浏览器 MCP 工具（原生客户端）。"""
    configurable = Configuration.from_runnable_config(config)
    if not configurable.browser_mcp_enabled:
        return []
    browser_config = configurable.browser_mcp_config or BrowserMCPConfig()
    allowed_names = set(browser_config.tools or [])
    if not allowed_names:
        return []
    if not allowed_names.issubset(browser_config.tool_effects):
        logger.warning(
            "Blocked browser MCP discovery because one or more tool effects are undeclared"
        )
        return []
    is_http_surface = config.get("metadata", {}).get("deployment_surface") == "http"
    if (
        is_http_surface
        and browser_config.transport == "stdio"
        and not configurable.allow_http_stdio_mcp
    ):
        logger.warning("Blocked browser stdio MCP on HTTP surface")
        return []
    if is_http_surface and browser_config.url:
        allowed_servers = {value.rstrip("/") for value in configurable.allowed_mcp_servers}
        if browser_config.url.rstrip("/") not in allowed_servers:
            logger.warning("Blocked non-allowlisted browser MCP server on HTTP surface")
            return []
    connection = _build_browser_connection(browser_config)
    if not connection:
        return []
    allowed_names = _discovery_tool_names(config, allowed_names, browser_config.tool_effects, ToolOrigin.BROWSER, role)
    if not allowed_names:
        return []
    if browser_config.url and not await _authorize_discovery_url(browser_config.url, config):
        return []
    connection["restricted_network"] = _restricted_network(config)
    try:
        server, available_tools = await _discover_via_server(connection)
    except Exception:  # noqa: BLE001 - 发现失败按无工具装载（fail-closed）
        return []

    loaded: list[Tool] = []
    for descriptor in available_tools:
        if descriptor.name in existing_tool_names:
            warnings.warn(
                f"Browser MCP tool '{descriptor.name}' conflicts with existing tool name - skipping"
            )
            continue
        if descriptor.name not in allowed_names:
            continue
        effect_value = browser_config.tool_effects.get(descriptor.name)
        if effect_value is None:
            warnings.warn(
                f"Browser MCP tool '{descriptor.name}' has no explicit tool_effects entry - skipping"
            )
            continue
        if not _description_is_safe(descriptor, configurable):
            warnings.warn(
                f"Browser MCP tool '{descriptor.name}' has an instruction-shaped description - skipping"
            )
            continue
        effect = ToolEffect(effect_value)
        if (
            configurable.web_pipeline_mode == "enforced"
            and effect is not ToolEffect.READ_ONLY
        ):
            continue
        loaded.append(
            NativeMcpTool(
                server,
                descriptor,
                origin=ToolOrigin.BROWSER,
                effect=effect,
                retryable=effect in {ToolEffect.READ_ONLY, ToolEffect.SENSITIVE_READ},
                egress_urls_url=browser_config.url,
            )
        )
    if not (loaded and server.client.is_stateful):
        # 产出工具的 stdio 服务器保持打开供调用方共享使用，释放入口是
        # ``close_native_mcp_tools``；其余情况立即关闭，不悬挂子进程。
        await server.close()
    return loaded


def native_skill_guidance(config: dict[str, Any]) -> str:
    """技能是纯上下文包：只贡献提示词，不贡献工具、不扩大权限。"""
    configurable = Configuration.from_runnable_config(config)
    return get_skill_researcher_context(configurable.skills)


__all__ = [
    "MCPInteractionRequired",
    "NativeMcpServer",
    "NativeMcpTool",
    "NativeMcpToolError",
    "build_args_schema",
    "build_native_mcp_client",
    "close_native_mcp_tools",
    "exchange_mcp_subject_token",
    "fetch_tokens",
    "get_tokens",
    "load_native_browser_mcp_tools",
    "load_native_mcp_tools",
    "native_skill_guidance",
    "set_tokens",
    "translate_mcp_interaction",
]
