"""Run-bound external extraction and governed browser rendering adapters."""

from __future__ import annotations

import asyncio
import re

import aiohttp

from open_deep_research.agentscope_runtime.search_providers import (
    SearchProviderError,
    candidate,
    source_allowed,
)
from open_deep_research.configuration import Configuration
from open_deep_research.models.resolution import resolve_named_api_key
from open_deep_research.sandbox.egress_context import authorize_url, egress_authorizer
from open_deep_research.security.network import (
    PublicWebResolver,
    validate_public_http_url,
)
from open_deep_research.web.extraction import text_document
from open_deep_research.web.fetching import _robots_allowed


async def tavily_extract(url, client_factory, config=None):
    """Extract using the active run's credentials, returning a normalized document."""
    config = config or {}
    if (
        egress_authorizer.get() is not None
        and await authorize_url(url, "external.extract", consume=True) != "allow"
    ):
        raise PermissionError("external_extract_not_allowed")
    result = await client_factory(config).extract(urls=[url], format="markdown")
    rows = result.get("results") or []
    if not rows or not str(rows[0].get("raw_content") or "").strip():
        return None
    row = rows[0]
    source = candidate("direct", url, str(row.get("title") or url), "", 1, "direct")
    return text_document(
        source,
        str(row["raw_content"]),
        str(row.get("url") or url),
        "text/markdown",
        "tavily_extract",
    )


async def firecrawl_extract(url, config, resources):
    """Resolve the current run key rather than a detached optional argument."""
    key = resolve_named_api_key("FIRECRAWL_API_KEY", config)
    if not key:
        raise SearchProviderError("missing_credential")
    endpoint = "https://api.firecrawl.dev/v1/scrape"
    if (
        egress_authorizer.get() is not None
        and await authorize_url(endpoint, "external.extract", consume=True) != "allow"
    ):
        raise PermissionError("external_service_not_allowed")
    client = resources.client("firecrawl", config)
    async with client.stream(
        "POST",
        endpoint,
        headers={"Authorization": f"Bearer {key}"},
        json={"url": url, "formats": ["markdown"]},
    ) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > Configuration.from_runnable_config(config).html_max_bytes:
                raise SearchProviderError("response_too_large")
        import json

        data = json.loads(body).get("data") or {}
    content = str(data.get("markdown") or "")
    if not content.strip():
        return None
    metadata = data.get("metadata") or {}
    source = candidate(
        "direct", url, str(metadata.get("title") or url), "", 1, "direct"
    )
    return text_document(
        source,
        content,
        str(metadata.get("sourceURL") or url),
        "text/markdown",
        "firecrawl",
    )


class BrowserRenderer:
    """Serialize navigation and snapshot on one authorized browser MCP session."""

    def __init__(self, tools, config):
        self.tools = {tool.name: tool for tool in tools}
        self.config = config
        self.lock = asyncio.Lock()

    async def __call__(self, url):
        from open_deep_research.tools.base import ToolEffect
        from open_deep_research.tools.governance import (
            AgentRole,
            execute_governed_tool_call_native,
        )

        names = {"browser_navigate", "browser_snapshot"}
        if not names.issubset(self.tools) or any(
            self.tools[n].effect is not ToolEffect.READ_ONLY for n in names
        ):
            raise SearchProviderError("browser_tools_unavailable")
        outputs = []
        async with self.lock:
            for name, args in (
                ("browser_navigate", {"url": url}),
                ("browser_snapshot", {}),
            ):
                result = await execute_governed_tool_call_native(
                    {"name": name, "id": name, "args": args},
                    self.tools,
                    AgentRole(
                        self.config.get("metadata", {}).get("tool_role", "researcher")
                    ),
                    self.config,
                    allowed_tools=names,
                    apply_retry=False,
                    operation_id=self.config.get("metadata", {}).get(
                        "tool_operation_id"
                    ),
                )
                if result.error:
                    raise PermissionError("browser_" + result.error.error_type.value)
                outputs.append(str(result.result.output))
        combined = "\n".join(outputs)
        urls = re.findall(r"(?im)^\s*(?:-\s*)?Page URL:\s*(https?://\S+)", combined)
        if not urls:
            raise SearchProviderError("browser_final_url_missing")
        final_url = urls[-1].rstrip("`\"'")
        if not source_allowed(final_url, self.config):
            raise PermissionError("source_scope_denied")
        await validate_public_http_url(final_url)
        if (
            egress_authorizer.get() is not None
            and await authorize_url(final_url) != "allow"
        ):
            raise PermissionError("browser_redirect_not_allowed")
        titles = re.findall(r"(?im)^\s*(?:-\s*)?Page Title:\s*(.+)", combined)
        source = candidate(
            "direct", url, titles[-1] if titles else url, "", 1, "direct"
        )
        return text_document(
            source, outputs[-1], final_url, "text/plain", "playwright_mcp"
        )


def configured_fetch_backends(config, settings, resources, browser_tools=()):
    """Build the same credential- and policy-bound adapters for both web tools."""
    cfg = Configuration.from_runnable_config(config)
    renderer = BrowserRenderer(browser_tools, config)

    async def gate(url):
        if not source_allowed(url, config):
            raise PermissionError("source_scope_denied")
        if (
            egress_authorizer.get() is not None
            and await authorize_url(url, "tool.egress", consume=True) != "allow"
        ):
            raise PermissionError("egress_not_allowed")
        # Authorization precedes DNS and robots requests even for remote extraction.
        try:
            await validate_public_http_url(url)
        except ValueError as exc:
            raise PermissionError("unsafe_url") from exc
        if settings.respect_robots_txt:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=settings.timeout_seconds),
                connector=aiohttp.TCPConnector(resolver=PublicWebResolver()),
            ) as session:
                if not await _robots_allowed(session, url, settings):
                    raise PermissionError("robots_disallowed")

    async def invoke(name, url):
        if name == "playwright" and (
            not cfg.browser_mcp_enabled or not cfg.browser_render_fallback_enabled
        ):
            raise SearchProviderError("browser_render_disabled")
        if name != "playwright" and name not in cfg.external_extract_backends:
            raise SearchProviderError("extractor_disabled")
        # Check availability before incurring any target network requests.
        if name == "tavily_extract":
            resources.client("tavily", config)
        if name == "firecrawl" and not resolve_named_api_key(
            "FIRECRAWL_API_KEY", config
        ):
            raise SearchProviderError("missing_credential")
        if name == "playwright" and not {
            "browser_navigate",
            "browser_snapshot",
        }.issubset(renderer.tools):
            raise SearchProviderError("browser_tools_unavailable")
        await gate(url)
        if name == "playwright":
            return await renderer(url)
        if name == "tavily_extract":
            if (
                egress_authorizer.get() is not None
                and await authorize_url(
                    "https://api.tavily.com/extract", "external.extract", consume=True
                )
                != "allow"
            ):
                raise PermissionError("external_service_not_allowed")
            document = await tavily_extract(
                url, lambda current: resources.client("tavily", current), config
            )
        else:
            document = await firecrawl_extract(url, config, resources)
        if document is not None:
            if not source_allowed(document.final_url, config):
                raise PermissionError("source_scope_denied")
            await validate_public_http_url(document.final_url)
            if (
                egress_authorizer.get() is not None
                and await authorize_url(document.final_url) != "allow"
            ):
                raise PermissionError("external_redirect_not_allowed")
        return document

    return {
        name: (lambda url, name=name: invoke(name, url))
        for name in cfg.fetch_backend_order
        if name != "local"
    }
