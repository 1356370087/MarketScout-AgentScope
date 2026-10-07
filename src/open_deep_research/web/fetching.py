"""Bounded HTTP fetching with robots, redirect and public-network checks."""

from __future__ import annotations

import asyncio
import hashlib
import urllib.robotparser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

from open_deep_research.sandbox.egress_context import authorize_url, egress_authorizer
from open_deep_research.security.network import (
    PublicWebResolver,
    validate_public_http_url,
    validate_response_peer,
)
from open_deep_research.web.models import CandidateSource, FetchResult
from open_deep_research.web.settings import WebPipelineSettings
from open_deep_research.web.sources import canonicalize_url

_ROBOTS_CACHE: dict[tuple[str, str], bool] = {}


def clear_robots_cache(run_id: str) -> None:
    """Release the owner's cached robots decisions."""
    for key in [key for key in _ROBOTS_CACHE if key[0] == run_id]:
        _ROBOTS_CACHE.pop(key, None)


@dataclass(slots=True)
class RawFetch:
    """Private in-memory response body; never emitted to model-facing state."""

    result: FetchResult
    body: bytes = b""


def _decode_body(body: bytes, charset: str | None) -> str:
    for encoding in (charset, "utf-8", "gb18030", "latin-1"):
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except LookupError, UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


async def _robots_allowed(
    session: aiohttp.ClientSession,
    url: str,
    settings: WebPipelineSettings,
) -> bool:
    """Check and cache robots.txt for a target host without persisting content."""
    if not settings.respect_robots_txt:
        return True
    parsed = urlsplit(url)
    # robots rules are path-sensitive; caching one boolean for an entire host
    # can allow a disallowed path after an allowed path was visited.
    key = (settings.cache_namespace, canonicalize_url(url))
    if key in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[key]
    robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
    try:
        if (
            egress_authorizer.get() is not None
            and await authorize_url(robots_url, consume=True) != "allow"
        ):
            raise PermissionError("egress_approval_required_or_denied")
        await validate_public_http_url(robots_url)
        async with session.get(robots_url, allow_redirects=False) as response:
            validate_response_peer(response)
            if response.status >= 400:
                allowed = True
            else:
                body = await response.content.read(256 * 1024 + 1)
                if len(body) > 256 * 1024:
                    allowed = False
                else:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(_decode_body(body, response.charset).splitlines())
                    allowed = parser.can_fetch(settings.user_agent, url)
    except TimeoutError, aiohttp.ClientError, ValueError:
        # A missing/unavailable robots file is not interpreted as a disallow.
        allowed = True
    _ROBOTS_CACHE[key] = allowed
    return allowed


async def _fetch_local_once(
    candidate: CandidateSource,
    settings: WebPipelineSettings,
    *,
    redirect_allowed: Callable[[str], Awaitable[bool]] | None = None,
) -> RawFetch:
    """Fetch one URL with bounded redirects, decompressed-size limits, and SSRF checks."""
    requested = candidate.canonical_url
    current = requested
    redirects: list[str] = []
    timeout = aiohttp.ClientTimeout(total=settings.timeout_seconds)
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.1",
    }
    result = FetchResult(candidate_id=candidate.candidate_id, requested_url=requested)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            auto_decompress=True,
            connector=aiohttp.TCPConnector(resolver=PublicWebResolver()),
        ) as session:
            for redirect_index in range(settings.max_redirects + 1):
                if (
                    egress_authorizer.get() is not None
                    and await authorize_url(current, consume=True) != "allow"
                ):
                    raise PermissionError("egress_approval_required_or_denied")
                if not await _robots_allowed(session, current, settings):
                    raise PermissionError("robots_disallowed")
                await validate_public_http_url(current)
                if (
                    egress_authorizer.get() is not None
                    and await authorize_url(current, consume=True) != "allow"
                ):
                    raise PermissionError("egress_approval_required_or_denied")
                async with session.get(current, allow_redirects=False) as response:
                    validate_response_peer(response)
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or redirect_index >= settings.max_redirects:
                            raise RuntimeError("redirect_limit")
                        target = canonicalize_url(urljoin(current, location))
                        if (
                            urlsplit(target).hostname,
                            urlsplit(target).port,
                            urlsplit(target).scheme,
                        ) != (
                            urlsplit(current).hostname,
                            urlsplit(current).port,
                            urlsplit(current).scheme,
                        ):
                            if egress_authorizer.get() is not None:
                                approved = await authorize_url(target) == "allow"
                            else:
                                approved = (
                                    redirect_allowed is not None
                                    and await redirect_allowed(target)
                                )
                            if not approved:
                                raise PermissionError(
                                    "cross_domain_redirect_not_approved"
                                )
                        redirects.append(target)
                        current = target
                        continue
                    if response.status >= 400:
                        raise RuntimeError(f"http_{response.status}")
                    content_type = response.headers.get("Content-Type", "").lower()
                    max_bytes = (
                        settings.pdf_max_bytes
                        if "application/pdf" in content_type
                        else settings.html_max_bytes
                    )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.content.iter_chunked(16_384):
                        size += len(chunk)
                        if size > max_bytes:
                            raise RuntimeError("response_too_large")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    digest = hashlib.sha256(body).hexdigest()
                    result = FetchResult(
                        candidate_id=candidate.candidate_id,
                        requested_url=requested,
                        final_url=current,
                        redirect_chain=redirects,
                        status_code=response.status,
                        content_type=content_type.split(";", 1)[0],
                        byte_count=len(body),
                        content_hash=digest,
                        fetched_at=datetime.now(UTC),
                        adapter="local",
                        success=True,
                    )
                    return RawFetch(result=result, body=body)
        raise RuntimeError("redirect_limit")
    except PermissionError as exc:
        result.failure_class = (
            "robots_disallowed"
            if "robots_disallowed" in str(exc)
            else "approval_required"
        )
        result.failure_message = str(exc)
    except TimeoutError as exc:
        result.failure_class = "timeout"
        result.failure_message = str(exc)
    except ValueError as exc:
        result.failure_class = (
            "network_error" if "could not be resolved" in str(exc) else "unsafe_url"
        )
        result.failure_message = str(exc)[:500]
    except (aiohttp.ClientError, RuntimeError) as exc:
        result.failure_class = (
            str(exc) if isinstance(exc, RuntimeError) else "network_error"
        )
        result.failure_message = str(exc)[:500]
    return RawFetch(result=result)


async def fetch_local(
    candidate: CandidateSource,
    settings: WebPipelineSettings,
    *,
    redirect_allowed: Callable[[str], Awaitable[bool]] | None = None,
) -> RawFetch:
    """Fetch with two bounded retries for transient network/429/5xx failures."""
    last: RawFetch | None = None
    for attempt in range(1, 4):
        last = await _fetch_local_once(
            candidate, settings, redirect_allowed=redirect_allowed
        )
        last.result.attempts = attempt
        if last.result.success:
            return last
        failure = last.result.failure_class or ""
        retryable = failure in {"timeout", "network_error"} or failure == "http_429"
        if failure.startswith("http_5"):
            retryable = True
        if not retryable or attempt >= 3:
            return last
        await asyncio.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
    return last or RawFetch(
        result=FetchResult(
            candidate_id=candidate.candidate_id,
            requested_url=candidate.canonical_url,
            failure_class="network_error",
            failure_message="fetch did not run",
        )
    )
