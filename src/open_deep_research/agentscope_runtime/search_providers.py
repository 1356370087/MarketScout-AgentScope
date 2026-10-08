"""Shared provider strategies and bounded, deterministic parallel discovery."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
from collections.abc import Callable
from itertools import zip_longest
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from open_deep_research.configuration import Configuration
from open_deep_research.documents.contracts import (
    SourceMode,
    selection_from_config,
    source_url_identity,
)
from open_deep_research.models.resolution import resolve_named_api_key
from open_deep_research.sandbox.egress_context import authorize_url, egress_authorizer
from open_deep_research.security.network import validate_http_url_syntax
from open_deep_research.web.models import (
    CandidateSource,
    ProviderSynthesis,
    SearchBatch,
    SearchDiscovery,
    SearchProviderResult,
    SearchRequest,
)
from open_deep_research.web.sources import (
    PROVIDER_URLS,
    canonicalize_url,
    normalize_candidates,
    stable_id,
)

MAX_SPECIFIC_DOMAIN_QUERIES = 24


class SearchProviderError(RuntimeError):
    """Public, credential-free provider failure code."""


def preserve_control_error(exc: BaseException) -> None:
    """Do not downgrade cancellation, lease loss or unknown execution to content."""
    from open_deep_research.tools.governance import _is_runtime_control_error

    if getattr(exc, "uncertain", False):
        from open_deep_research.agentscope_runtime.recovery_store import (
            UnknownOperation,
        )

        raise UnknownOperation(str(exc)) from exc
    if not isinstance(exc, Exception) or _is_runtime_control_error(exc):
        raise exc


def error_code(exc: Exception) -> str:
    """Normalize external errors without exposing keys or response bodies."""
    if isinstance(exc, SearchProviderError):
        return str(exc)
    if isinstance(exc, ValueError) and str(exc).endswith("_search_model_required"):
        return str(exc)
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status in {401, 403}:
        return "authentication_failed"
    if status == 429:
        return "rate_limited"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if status:
        return f"http_{status}"
    return type(exc).__name__


def candidate(
    provider: str,
    url: str,
    title: str,
    snippet: str,
    rank: int,
    query: str,
    content_hint: str | None = None,
) -> CandidateSource | None:
    """Normalize a provider URL at the external data boundary."""
    try:
        validate_http_url_syntax(url)
        canonical = canonicalize_url(url)
    except TypeError, ValueError:
        return None
    return CandidateSource(
        candidate_id=stable_id("src", canonical),
        provider=provider,
        query_ids=[query],
        provider_rank=rank,
        original_url=url,
        canonical_url=canonical,
        domain=urlsplit(canonical).hostname or "",
        title=title,
        snippet=snippet,
        content_hint=content_hint,
        discoveries=[SearchDiscovery(provider=provider, query=query, rank=rank)],
    )


def bounded_specific_queries(
    domains: list[str], queries: list[str]
) -> tuple[list[str], bool, int]:
    """Apply the existing total query cap without starving later domains."""
    expanded = [f"site:{domain} {query}" for domain in domains for query in queries]
    if len(expanded) <= MAX_SPECIFIC_DOMAIN_QUERIES:
        return expanded, False, len(expanded)
    return (
        [f"site:{domain} {query}" for query in queries for domain in domains][
            :MAX_SPECIFIC_DOMAIN_QUERIES
        ],
        True,
        len(expanded),
    )


def domain_matches(host: str, domains: list[str] | tuple[str, ...]) -> bool:
    """Match a domain or its subdomains, never a suffix-lookalike host."""
    return any(
        host == d.lower().rstrip(".") or host.endswith("." + d.lower().rstrip("."))
        for d in domains
    )


def source_allowed(
    url: str, config: dict, request: SearchRequest | None = None
) -> bool:
    """Apply the run's source boundary to discovery and final fetch URLs."""
    try:
        validate_http_url_syntax(url)
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    selection = selection_from_config(config)
    if not selection.web_enabled:
        return False
    contract = config.get("metadata", {}).get("coverage_contract")
    if contract:
        from open_deep_research.evidence import (
            SourceScopeStatus,
            classify_evidence_source,
        )

        decision = classify_evidence_source({"source_url": url}, contract)
        if decision.source_scope_status not in {
            SourceScopeStatus.IN_SCOPE, SourceScopeStatus.NOT_CONSTRAINED,
        }:
            return False
    if selection.mode is SourceMode.SPECIFIC:
        exact = {source_url_identity(item) for item in selection.urls}
        if source_url_identity(url) not in exact and not domain_matches(
            host, selection.domains
        ):
            return False
    if request is not None:
        if request.allowed_domains and not domain_matches(
            host, request.allowed_domains
        ):
            return False
        if domain_matches(host, request.blocked_domains):
            return False
    return True


def parse_openai_search(response) -> tuple[str, list[dict[str, str]]]:
    """Read Responses search citations from SDK objects or persisted JSON."""
    data = response.model_dump() if hasattr(response, "model_dump") else response

    def get(value, key, default=None):
        return (
            value.get(key, default)
            if isinstance(value, dict)
            else getattr(value, key, default)
        )

    text = str(get(data, "output_text", "") or "")
    sources = []
    parts = []
    for item in get(data, "output", []) or []:
        if get(item, "type") != "message":
            continue
        for part in get(item, "content", []) or []:
            if get(part, "text"):
                parts.append(str(get(part, "text")))
            for annotation in get(part, "annotations", []) or []:
                if get(annotation, "url"):
                    sources.append(
                        {
                            "url": str(get(annotation, "url")),
                            "title": str(
                                get(annotation, "title") or get(annotation, "url")
                            ),
                        }
                    )
    return text or "\n".join(parts), sources


def parse_anthropic_search(response) -> tuple[str, list[dict[str, str]]]:
    """Read Anthropic results, treating server-tool errors as provider failures."""

    def get(value, key, default=None):
        return (
            value.get(key, default)
            if isinstance(value, dict)
            else getattr(value, key, default)
        )

    parts, sources = [], []
    for block in get(response, "content", []) or []:
        if get(block, "type") == "text":
            parts.append(str(get(block, "text", "")))
        elif get(block, "type") == "web_search_tool_result":
            results = get(block, "content", [])
            if not isinstance(results, (list, tuple)):
                raise SearchProviderError(
                    "server_search_" + str(get(results, "error_code", "failed"))
                )
            for item in results:
                if get(item, "url"):
                    sources.append(
                        {
                            "url": str(get(item, "url")),
                            "title": str(get(item, "title") or get(item, "url")),
                        }
                    )
    return "\n".join(parts), sources


def deduplicate_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep the first occurrence of each citation URL."""
    return list({item["url"]: item for item in reversed(sources)}.values())[::-1]


def resolve_bing_url(url: str) -> str:
    """Decode Bing's URL-safe base64 redirect payload before URL validation."""
    parsed = urlsplit(url)
    if parsed.hostname in {"www.bing.com", "bing.com"} and parsed.path.startswith(
        "/ck/"
    ):
        value = parse_qs(parsed.query).get("u", [""])[0]
        if value.startswith(("a1", "a0")):
            value = value[2:]
        try:
            url = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode(
                "utf-8"
            )
        except ValueError, UnicodeError:
            return ""
    return url


def parse_bing_results(html: str) -> list[dict[str, str]]:
    """Parse organic hits and distinguish empty results from challenge pages."""
    soup = BeautifulSoup(html, "html.parser")
    if (
        soup.select_one("#b_captcha, #captcha, form[action*='challenge']")
        or "verify you are human" in soup.get_text(" ", strip=True).lower()
    ):
        raise SearchProviderError("captcha_required")
    results = []
    for block in soup.select("li.b_algo"):
        link = block.select_one("h2 a[href]")
        if link is None:
            continue
        snippet = (
            block.select_one("p[class*='b_lineclamp']")
            or block.select_one(".b_caption p")
            or block.select_one(".b_caption")
        )
        results.append(
            {
                "url": resolve_bing_url(str(link.get("href", ""))),
                "title": link.get_text(" ", strip=True),
                "content": snippet.get_text(" ", strip=True) if snippet else "",
            }
        )
    if not results and not soup.select_one(".b_no"):
        raise SearchProviderError("search_page_unrecognized")
    return results


class SearchResources:
    """Small request/run-owned client dictionary, closed by the resource owner."""

    def __init__(self, client_factories: dict[str, Callable] | None = None):
        self.factories = client_factories or {}
        self.clients = {}

    def client(self, provider: str, config: dict):
        if provider not in self.clients:
            if provider in self.factories:
                self.clients[provider] = self.factories[provider](config)
            elif provider == "tavily":
                from tavily import AsyncTavilyClient

                key = resolve_named_api_key("TAVILY_API_KEY", config)
                if not key:
                    raise SearchProviderError("missing_credential")
                self.clients[provider] = AsyncTavilyClient(api_key=key)
            else:
                self.clients[provider] = httpx.AsyncClient(
                    timeout=30.0, follow_redirects=False
                )
        return self.clients[provider]

    async def get(self, provider: str, config: dict, *, return_info=False, **kwargs):
        """Read a fixed provider endpoint with a bounded response body."""
        client = self.client(provider, config)
        url = PROVIDER_URLS[provider]
        modern = config.get("metadata", {}).get("run_config_schema_version", 18) >= 18
        for hop in range(3):
            async with client.stream("GET", url, **kwargs) as response:
                if response.is_redirect:
                    target = urljoin(str(response.url), response.headers.get("location", ""))
                    parsed = urlsplit(target)
                    if not modern or provider != "bing" or hop == 2 or parsed.scheme != "https" or parsed.hostname not in {"www.bing.com", "cn.bing.com"} or parsed.path != "/search":
                        raise SearchProviderError("provider_redirect_refused")
                    if egress_authorizer.get() is not None and await authorize_url(target, "search.provider", consume=True) != "allow":
                        raise SearchProviderError("provider_redirect_not_allowed")
                    url = target
                    kwargs.pop("params", None)
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise SearchProviderError("provider_response_too_large")
                text = body.decode("utf-8", errors="replace")
                info = {"http_status": response.status_code, "final_endpoint": str(response.url).split("?")[0], "redirect_count": hop}
                return (text, info) if return_info else text


    async def aclose(self):
        for client in self.clients.values():
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close:
                result = close()
                if inspect.isawaitable(result):
                    await result
        self.clients.clear()


def bing_query(query, domains, *, simplified=False):
    """Keep the product name instead of an unsupported site-only query."""
    sites = re.findall(r"site:([^\s]+)", query, re.IGNORECASE)
    text = re.sub(r"site:[^\s]+", "", query, flags=re.IGNORECASE).strip()
    entity = (sites[0] if sites else "").removeprefix("www.").split(".")[0]
    if entity and entity.casefold() not in text.casefold():
        text = entity + " " + text
    words = text.split()
    return " ".join(words[:3] + ["documentation"]) if simplified else " ".join(words[:8])


class SearchService:
    """Provider strategies share filtering, concurrency and deterministic merging."""

    def __init__(
        self,
        config: dict,
        models,
        resources: SearchResources,
        *,
        progress=None,
        legacy=False,
    ):
        self.config, self.models, self.resources = config, models, resources
        self.settings = Configuration.from_runnable_config(config)
        self.progress, self.legacy = progress, legacy

    async def _progress(self, phase: str, **payload):
        if self.progress:
            await self.progress(phase, **payload)

    async def _tavily(self, query: str, request: SearchRequest):
        options = {
            "max_results": min(10, request.candidate_limit),
            "topic": request.topic,
            "include_raw_content": self.legacy,
        }
        if request.allowed_domains:
            options["include_domains"] = request.allowed_domains
        if request.blocked_domains:
            options["exclude_domains"] = request.blocked_domains
        response = await self.resources.client("tavily", self.config).search(
            query, **options
        )
        return response.get("results", []), ""

    async def _bing(self, query: str, request: SearchRequest):
        value = await self.resources.get(
            "bing",
            self.config,
            return_info=True,
            params={"q": query, "setmkt": request.locale or "en-US"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
                "Accept": "text/html",
                "Accept-Language": request.locale or "en-US,en;q=0.9",
            },
        )
        html, info = value if isinstance(value, tuple) else (value, {})
        return parse_bing_results(html), "", info

    async def _brave(self, query: str, request: SearchRequest):
        key = resolve_named_api_key("BRAVE_API_KEY", self.config)
        if not key:
            raise SearchProviderError("missing_credential")
        text = await self.resources.get(
            "brave",
            self.config,
            params={"q": query, "count": min(20, request.candidate_limit)},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
        )
        data = json.loads(text)
        return [
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "content": item.get("description", ""),
            }
            for item in (data.get("web") or {}).get("results", [])
        ], ""

    async def _model_search(self, provider: str, query: str, request: SearchRequest):
        model = self.settings.search_model(provider)
        # Explicit SDK factories are an injection seam for offline contract tests.
        # Production always uses the native ModelFactory server-search port.
        if provider in self.resources.factories:
            client = self.resources.client(provider, self.config)
            if provider == "openai":
                response = await client.responses.create(
                    model=model.split(":", 1)[1],
                    input=query,
                    tools=[{"type": "web_search_preview"}],
                )
                text, sources = parse_openai_search(response)
            else:
                response = await client.messages.create(
                    model=model.split(":", 1)[1],
                    max_tokens=self.settings.research_model_max_tokens,
                    messages=[{"role": "user", "content": query}],
                    tools=[
                        {
                            "type": "web_search_20250305",
                            "name": "web_search",
                            "max_uses": 5,
                        }
                    ],
                )
                text, sources = parse_anthropic_search(response)
        else:
            result = await self.models.search_web(
                provider,
                query,
                allowed_domains=request.allowed_domains,
                blocked_domains=request.blocked_domains,
                progress=self.progress,
                logical_operation_id=stable_id(
                    "search",
                    f"{self.config.get('metadata', {}).get('tool_operation_id', '')}:{provider}:{query}",
                ),
                trace_metadata={
                    "tool_call_id": str(
                        self.config.get("metadata", {}).get("tool_call_id", "")
                    )
                },
            )
            text, sources = result["text"], result["sources"]
        return deduplicate_sources(sources), text

    async def discover(self, request: SearchRequest) -> SearchBatch:
        """Search all selected providers, retaining successes when peers fail."""
        selection = selection_from_config(self.config)
        exact = [
            item
            for url in selection.urls
            if (
                item := candidate(
                    "specific_url",
                    url,
                    url,
                    "Explicit URL selected by the user",
                    1,
                    "specific-url",
                )
            )
        ]
        if not selection.web_enabled:
            return SearchBatch(errors=["web_sources_disabled"])
        providers = [item.value for item in self.settings.resolved_search_providers]
        if not providers or (
            selection.mode is SourceMode.SPECIFIC and not selection.domains
        ):
            return SearchBatch(
                candidates=exact, errors=[] if exact else ["search_api_none"]
            )
        errors = []
        modern = self.config.get("metadata", {}).get("run_config_schema_version", 18) >= 18
        if selection.mode is SourceMode.SPECIFIC and selection.domains and modern:
            request = request.model_copy(update={"allowed_domains": list(selection.domains)})
        elif selection.mode is SourceMode.SPECIFIC and selection.domains:
            queries, overflow, total = bounded_specific_queries(
                list(selection.domains), request.queries
            )
            request = request.model_copy(
                update={"queries": queries, "allowed_domains": list(selection.domains)}
            )
            if overflow:
                errors.append(
                    f"specific_domain_query_limit_exceeded:{total}:{MAX_SPECIFIC_DOMAIN_QUERIES}"
                )
        semaphore = asyncio.Semaphore(self.settings.search_max_concurrency)
        strategies = {"tavily": self._tavily, "bing": self._bing, "brave": self._brave}

        async def run(provider, index, query):
            async with semaphore:
                await self._progress(
                    "query_started", provider=provider, query=query, query_index=index
                )
                try:
                    # Native model search performs service admission at its model port.
                    if (
                        provider not in {"openai", "anthropic"}
                        and egress_authorizer.get() is not None
                    ) and (
                        await authorize_url(
                            PROVIDER_URLS[provider], "search.provider", consume=True
                        )
                        != "allow"
                    ):
                        raise SearchProviderError("provider_egress_not_allowed")
                    effective_query = bing_query(query, request.allowed_domains) if modern and provider == "bing" else query
                    result = await (
                        strategies[provider](effective_query, request)
                        if provider in strategies
                        else self._model_search(provider, query, request)
                    )
                    rows, synthesis, *details = result
                    parsed_count = 0
                    filter_reasons = {}
                    def project(rows):
                        nonlocal parsed_count
                        items = []
                        limit = min(10, request.candidate_limit)
                        if len(rows) > limit:
                            filter_reasons["candidate_limit"] = filter_reasons.get("candidate_limit", 0) + len(rows) - limit
                        for rank, row in enumerate(rows[:min(10, request.candidate_limit)], 1):
                            item = candidate(provider, str(row.get("url", "")), str(row.get("title", "")),
                                str(row.get("content", "")), rank, query, row.get("raw_content"))
                            if item is None:
                                filter_reasons["invalid_url"] = filter_reasons.get("invalid_url", 0) + 1
                                continue
                            parsed_count += 1
                            if source_allowed(item.canonical_url, self.config, request):
                                items.append(item)
                            else:
                                filter_reasons["source_scope"] = filter_reasons.get("source_scope", 0) + 1
                        return items
                    items = project(rows)
                    raw_count = len(rows)
                    info = details[0] if details else {}
                    attempts = 1 + info.get("redirect_count", 0)
                    if modern and provider == "bing" and rows and not items:
                        retry_query = bing_query(query, request.allowed_domains, simplified=True)
                        if retry_query != effective_query:
                            if egress_authorizer.get() is not None and await authorize_url(PROVIDER_URLS[provider], "search.provider", consume=True) != "allow":
                                raise SearchProviderError("provider_egress_not_allowed")
                            rows, synthesis, *details = await self._bing(retry_query, request)
                            items = project(rows)
                            raw_count += len(rows)
                            info = details[0] if details else info
                            attempts += 1 + info.get("redirect_count", 0)
                    outcome = dict(
                        provider=provider,
                        query=query,
                        query_index=index,
                        result_count=len(items),
                        raw_result_count=raw_count, parsed_result_count=parsed_count,
                        filtered_result_count=raw_count-len(items),
                        filter_reasons=filter_reasons,
                        unique_result_count=len({i.canonical_url for i in items}),
                        result_status="usable" if items else "all_filtered" if raw_count else "empty",
                        provider_requests=attempts, effective_query=effective_query, **info,
                    )
                    await self._progress("query_completed", **outcome)
                    return provider, items, synthesis, None, raw_count, attempts, outcome
                except Exception as exc:  # noqa: BLE001 - preserve control errors before normalizing provider failures
                    preserve_control_error(exc)
                    code = error_code(exc)
                    await self._progress(
                        "provider_failed",
                        provider=provider,
                        query_index=index,
                        error_code=code,
                    )
                    return provider, [], "", code, 0, 1, {"query": query, "error_code": code, "result_status": "failed"}

        tasks = [
            asyncio.create_task(run(provider, index, query))
            for provider in providers
            for index, query in enumerate(request.queries)
        ]
        try:
            outcomes = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        by_provider = {provider: [] for provider in providers}
        failures = {provider: [] for provider in providers}
        syntheses = []
        for provider, items, text, error, raw_count, attempts, outcome in outcomes:
            by_provider[provider].extend(items)
            if error:
                failures[provider].append(error)
                errors.append(f"{provider}:{error}")
            if text:
                syntheses.append(
                    ProviderSynthesis(
                        provider=provider,
                        text=text[:10_000],
                        cited_candidate_ids=[item.candidate_id for item in items],
                    )
                )
        merged = [
            item
            for row in zip_longest(*(by_provider[p] for p in providers))
            for item in row
            if item is not None
        ]
        statuses = [
            SearchProviderResult(
                provider=p,
                query_count=len(request.queries),
                raw_result_count=sum(o[4] for o in outcomes if o[0] == p),
                provider_requests=sum(o[5] for o in outcomes if o[0] == p),
                unique_result_count=len({item.canonical_url for item in by_provider[p]}),
                exclusive_result_count=len({item.canonical_url for item in by_provider[p]} - {item.canonical_url for other in providers if other != p for item in by_provider[other]}),
                shared_result_count=len({item.canonical_url for item in by_provider[p]} & {item.canonical_url for other in providers if other != p for item in by_provider[other]}),
                duplicate_result_count=len(by_provider[p]) - len({item.canonical_url for item in by_provider[p]}),
                parsed_result_count=sum(o[6].get("parsed_result_count", 0) for o in outcomes if o[0] == p),
                filtered_result_count=sum(o[6].get("filtered_result_count", 0) for o in outcomes if o[0] == p),
                query_outcomes=[o[6] for o in outcomes if o[0] == p],
                result_count=len(by_provider[p]),
                status="failed"
                if len(failures[p]) == len(request.queries)
                else "partial"
                if failures[p]
                else "completed",
                error_codes=list(dict.fromkeys(failures[p])),
            )
            for p in providers
        ]
        return SearchBatch(
            candidates=normalize_candidates(exact + merged, request.candidate_limit),
            syntheses=syntheses,
            errors=errors,
            provider_results=statuses,
            search_calls=sum(o[5] for o in outcomes),
            raw_candidate_count=len(exact) + sum(o[4] for o in outcomes),
        )
