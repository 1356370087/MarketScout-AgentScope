"""Knowledge sync uses the same public DNS/socket and administrator policy gates."""

from urllib.parse import urlsplit

import aiohttp
import httpx

from open_deep_research.configuration import Configuration
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.sandbox.schema import network_target_decision, resolve_profile
from open_deep_research.security.network import (
    PublicWebResolver,
    validate_http_url_syntax,
    validate_response_peer,
)


def sync_authorizer(roles):
    """Standalone sync may use allowed targets; unresolved approval never auto-allows."""
    _, _, profile = resolve_profile(Configuration.from_runnable_config(None), roles=set(roles))

    async def authorize(url):
        validate_http_url_syntax(url)
        target = urlsplit(url)
        decision = network_target_decision(profile.network, target.hostname, target.port or (443 if target.scheme == "https" else 80))
        if "GET" not in profile.network.allow_http_methods or decision != "allow":
            raise PermissionError("knowledge_sync_target_requires_administrator_allowance")

    return authorize


class PublicSyncTransport(httpx.AsyncBaseTransport):
    """Validate the DNS answers used for dialing and the connected peer."""

    async def handle_async_request(self, request):
        validate_http_url_syntax(str(request.url))
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PublicWebResolver(), use_dns_cache=False),
            trust_env=False, timeout=aiohttp.ClientTimeout(total=30),
        ) as client, client.get(str(request.url), headers=dict(request.headers), allow_redirects=False) as response:
            validate_response_peer(response)
            body = bytearray()
            maximum = get_document_settings().max_file_bytes
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > maximum:
                    raise ValueError("knowledge_sync_file_too_large")
            headers = {key: value for key, value in response.headers.items() if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
            return httpx.Response(response.status, headers=headers,
                                  content=bytes(body), request=request)
