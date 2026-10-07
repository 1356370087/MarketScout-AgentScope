"""Deterministic Search -> Top-K Fetch -> Evidence pipeline."""

from __future__ import annotations

import asyncio
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

from open_deep_research.sandbox.egress_context import authorize_url, egress_authorizer
from open_deep_research.security.content import inspect_untrusted_content
from open_deep_research.web.extraction import (
    chunk_document,
    extract_document,
    extract_html,
    extract_pdf,
    needs_extraction_fallback,
)
from open_deep_research.web.fetching import RawFetch, clear_robots_cache, fetch_local
from open_deep_research.web.models import (
    BudgetSnapshot,
    CandidateSource,
    DocumentChunk,
    DomainApprovalBatch,
    EvidenceRecord,
    ExtractedDocument,
    FetchResult,
    GapAnalysis,
    RankedCandidate,
    SearchBatch,
    SearchRequest,
    WebResearchResult,
)
from open_deep_research.web.settings import WebPipelineSettings
from open_deep_research.web.sources import (
    canonicalize_url,
    normalize_candidates,
    stable_id,
)

TRACKING_PARAMS = {"gclid", "fbclid", "dclid", "msclkid", "mc_cid", "mc_eid"}
WORD_RE = re.compile(r"[\w\u3400-\u9fff]{2,}", re.UNICODE)
SENTENCE_RE = re.compile(r"(?<=[。！？.!?])\s+")
EVIDENCE_BLOCK_RE = re.compile(r"\n\s*\n+")
COMPLETE_SENTENCE_RE = re.compile(r"[。！？.!?](?:[\"'”’)\]}`*_]+)?$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
EVIDENCE_STOPWORDS = frozenset(
    {
        "a",
        "after",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "by",
        "extract",
        "find",
        "for",
        "format",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "pep",
        "read",
        "rule",
        "section",
        "specifically",
        "that",
        "the",
        "their",
        "this",
        "to",
        "verify",
        "was",
        "were",
        "with",
    }
)
JS_SHELL_MARKERS = (
    "enable javascript",
    "javascript is required",
    "please turn on javascript",
    "__next_data__",
    'id="root"',
    'id="app"',
)
_DOCUMENT_CACHE: dict[tuple[str, str], ExtractedDocument] = {}
_FETCH_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
# Transport-level failure classes that get refunded to the fetch budget and
# charged to the bounded transport-failure allowance instead. Content-level
# failures (HTTP status errors, robots/policy refusals, extraction problems)
# keep consuming the attempt budget as before.
TRANSPORT_FAILURE_CLASSES = frozenset({"timeout", "network_error"})


def clear_run_web_cache(run_id: str) -> None:
    """Remove run-scoped extracted documents, locks, and robots decisions."""
    clear_robots_cache(run_id)
    for mapping in (_DOCUMENT_CACHE, _FETCH_LOCKS):
        for key in [key for key in mapping if key[0] == run_id]:
            mapping.pop(key, None)


def cached_document(run_id: str, url: str, *, evidence: bool = True):
    """Read only usable cached documents; acquiring evidence may require a fallback."""
    value = _DOCUMENT_CACHE.get((run_id, canonicalize_url(url)))
    if value and needs_extraction_fallback(value, evidence=evidence):
        return None
    return value


class SearchAdapter(Protocol):
    """Provider-neutral candidate discovery contract."""

    async def __call__(self, request: SearchRequest) -> SearchBatch:
        """Return normalized candidates and optional non-evidence synthesis."""
        ...


class RerankAdapter(Protocol):
    """Optional semantic scorer used after deterministic filtering."""

    async def __call__(
        self, objective: str, candidates: list[CandidateSource]
    ) -> dict[str, tuple[float, float, float]]:
        """Return candidate_id -> relevance, authority, information gain."""
        ...


ApprovalAdapter = Callable[[list[CandidateSource], int], Awaitable[DomainApprovalBatch]]
DynamicRenderAdapter = Callable[[str], Awaitable[ExtractedDocument | None]]
ExternalExtractAdapter = Callable[[str], Awaitable[ExtractedDocument | None]]
EvidenceAdapter = Callable[
    [str, dict[str, ExtractedDocument], list[DocumentChunk]],
    Awaitable[list[EvidenceRecord]],
]


def _terms(text: str) -> set[str]:
    return {match.group(0).lower() for match in WORD_RE.finditer(text or "")}


def _evidence_terms(text: str) -> set[str]:
    """Return content-bearing terms for evidence coverage selection."""
    normalized: set[str] = set()
    for term in _terms(text):
        if term.isdigit() or term in EVIDENCE_STOPWORDS:
            continue
        if (
            term.isascii()
            and len(term) > 4
            and term.endswith("s")
            and not term.endswith(("ss", "us", "is"))
        ):
            term = term[:-1]
        if term not in EVIDENCE_STOPWORDS:
            normalized.add(term)
    return normalized


def _heuristic_authority(candidate: CandidateSource) -> float:
    domain = candidate.domain.lower()
    if domain.endswith((".gov", ".edu", ".ac.uk")):
        return 1.0
    trusted_hosts = ("doi.org", "arxiv.org", "who.int", "un.org", "europa.eu")
    if any(domain == host or domain.endswith(f".{host}") for host in trusted_hosts):
        return 0.9
    return 0.55


async def rank_candidates(
    objective: str,
    candidates: list[CandidateSource],
    *,
    reranker: RerankAdapter | None = None,
    top_k: int = 5,
    max_per_domain: int = 2,
    min_authority: float = 0.0,
) -> list[RankedCandidate]:
    """Apply stable rules, authority admission, and domain diversity."""
    semantic: dict[str, tuple[float, float, float]] = {}
    if reranker and candidates:
        try:
            semantic = await reranker(objective, candidates)
        except Exception as exc:  # noqa: BLE001 - deterministic content fallback only
            from open_deep_research.agentscope_runtime.search_providers import (
                preserve_control_error,
            )

            preserve_control_error(exc)
            semantic = {}
    objective_terms = _terms(objective)
    ranked: list[RankedCandidate] = []
    total = max(1, len(candidates))
    for position, candidate in enumerate(candidates):
        overlap = len(
            objective_terms & _terms(f"{candidate.title} {candidate.snippet}")
        )
        lexical = min(1.0, overlap / max(3, len(objective_terms) * 0.25))
        semantic_scores = semantic.get(candidate.candidate_id)
        relevance, authority, information_gain = semantic_scores or (
            lexical,
            _heuristic_authority(candidate),
            min(1.0, len(candidate.snippet) / 500),
        )
        rank_score = max(0.0, 1.0 - (position / total))
        freshness = 0.5
        score = (
            0.45 * relevance
            + 0.20 * authority
            + 0.15 * information_gain
            + 0.10 * freshness
            + 0.10 * rank_score
        )
        ranked.append(
            RankedCandidate(
                candidate=candidate,
                relevance=relevance,
                authority=authority,
                authority_method=(
                    "reranker" if semantic_scores is not None else "heuristic"
                ),
                information_gain=information_gain,
                freshness=freshness,
                provider_rank_score=rank_score,
                final_score=min(1.0, max(0.0, score)),
            )
        )
    ranked.sort(
        key=lambda item: (
            -item.final_score,
            item.candidate.provider_rank,
            item.candidate.canonical_url,
        )
    )
    domain_counts: Counter[str] = Counter()
    selected = 0
    for item in ranked:
        domain = item.candidate.domain
        # An exact user URL is an allowlist entry, not a discovery result;
        # retain its measured authority while bypassing this generic floor.
        if item.authority < min_authority and item.candidate.provider != "specific_url":
            item.reason = "below_authority_threshold"
        elif selected >= top_k:
            item.reason = "below_top_k"
        elif domain_counts[domain] >= max_per_domain:
            item.reason = "domain_diversity_limit"
        else:
            item.selected = True
            item.reason = "selected_top_k"
            domain_counts[domain] += 1
            selected += 1
    return ranked


def select_chunks(
    objective: str, chunks: list[DocumentChunk], limit: int
) -> list[DocumentChunk]:
    """Select relevant chunks without sending full documents to an LLM."""
    terms = _terms(objective)
    scored = [
        (
            len(terms & _terms(f"{chunk.heading or ''} {chunk.text}")),
            -chunk.start_offset,
            chunk,
        )
        for chunk in chunks
    ]
    scored.sort(key=lambda item: (-item[0], -item[1], item[2].chunk_id))
    return [item[2] for item in scored[:limit]]


def _safe_evidence_sentences(text: str) -> list[str]:
    """Keep factual sentences while quarantining local and cross-sentence attacks."""
    candidates: list[str] = []
    for block in EVIDENCE_BLOCK_RE.split(text):
        normalized_block = " ".join(
            line.strip(" -*\t") for line in block.splitlines() if line.strip(" -*\t")
        )
        candidates.extend(
            sentence
            for raw_sentence in SENTENCE_RE.split(normalized_block)
            if (sentence := " ".join(raw_sentence.split()).strip())
        )

    blocked = {
        index
        for index, sentence in enumerate(candidates)
        if inspect_untrusted_content(sentence)
    }
    for index in range(len(candidates) - 1):
        if index in blocked or index + 1 in blocked:
            continue
        if inspect_untrusted_content(f"{candidates[index]} {candidates[index + 1]}"):
            blocked.update({index, index + 1})
    return [
        sentence
        for index, sentence in enumerate(candidates)
        if index not in blocked and 40 <= len(sentence) <= 1000
    ]


def _select_evidence_sentences(
    objective: str,
    sentences: list[str],
    *,
    limit: int = 3,
) -> list[str]:
    """Select relevant sentences while rewarding new objective dimensions."""
    objective_terms = _evidence_terms(objective)
    remaining_terms = set(objective_terms)
    candidates = [
        (index, sentence, _evidence_terms(sentence) & objective_terms)
        for index, sentence in enumerate(sentences)
    ]
    if not objective_terms or not any(item[2] for item in candidates):
        return sentences[:limit]
    selected: list[str] = []
    while candidates and len(selected) < limit:
        best = max(
            candidates,
            key=lambda item: (
                len(item[2] & remaining_terms),
                len(item[2]),
                -len(item[1]),
                -item[0],
            ),
        )
        candidates.remove(best)
        selected.append(best[1])
        remaining_terms.difference_update(best[2])
    return selected


def evidence_from_chunks(
    objective: str,
    document_by_id: dict[str, ExtractedDocument],
    chunks: list[DocumentChunk],
) -> list[EvidenceRecord]:
    """Create bounded claim evidence while quarantining instruction-shaped chunks."""
    evidence: list[EvidenceRecord] = []
    for chunk in chunks:
        sentences = _safe_evidence_sentences(chunk.text)
        document = document_by_id[chunk.document_id]
        locator = (
            f"page {chunk.page}"
            if chunk.page
            else f"chars {chunk.start_offset}-{chunk.end_offset}"
        )
        for excerpt in _select_evidence_sentences(objective, sentences):
            evidence.append(
                EvidenceRecord(
                    evidence_id=stable_id("ev", f"{chunk.chunk_id}:{excerpt}"),
                    claim=excerpt,
                    supporting_excerpt=excerpt,
                    document_id=document.document_id,
                    chunk_id=chunk.chunk_id,
                    locator=locator,
                    source_url=document.final_url,
                    source_title=document.title,
                    confidence=0.7,
                )
            )
    return evidence


def _complete_model_evidence(record: EvidenceRecord) -> bool:
    """Reject model claims backed only by a heading or truncated line fragment."""
    claim = " ".join(record.claim.split()).strip()
    excerpt = " ".join(record.supporting_excerpt.split()).strip()
    if (
        not claim
        or not 40 <= len(excerpt) <= 1000
        or not COMPLETE_SENTENCE_RE.search(excerpt)
        or inspect_untrusted_content(excerpt)
    ):
        return False
    if claim.casefold() == excerpt.casefold():
        return True
    if excerpt.casefold() in claim.casefold():
        return len(excerpt) / max(1, len(claim)) >= 0.6
    claim_terms = _terms(claim)
    if not claim_terms:
        return False
    return len(claim_terms & _terms(excerpt)) / len(claim_terms) >= 0.5


def merge_evidence_records(
    primary: list[EvidenceRecord],
    deterministic: list[EvidenceRecord],
) -> list[EvidenceRecord]:
    """Keep model claims while filling omitted chunk facts with grounded evidence."""
    merged: list[EvidenceRecord] = []
    seen: set[tuple[str, str]] = set()
    grounded_primary = [
        record for record in primary if _complete_model_evidence(record)
    ]
    for record in [*grounded_primary, *deterministic]:
        excerpt_key = " ".join(record.supporting_excerpt.split()).casefold()
        key = (record.source_url, excerpt_key)
        if not excerpt_key or key in seen:
            continue
        seen.add(key)
        merged.append(record)
    return merged


def _budget_exhaustion_reason(budget: BudgetSnapshot) -> str:
    """Describe the actual budget wall that produced an exhausted snapshot."""
    if budget.exhaustion_cause == "transport_failures":
        return (
            "The transport-failure allowance is exhausted after repeated "
            "unreachable targets; timeout/connection/DNS failures no longer "
            "consume the fetch-attempt budget."
        )
    if budget.exhaustion_scope in {"run", "run_and_task"}:
        return "The run-level fetch-attempt budget is exhausted."
    if budget.exhaustion_scope == "task":
        return (
            "The fetch-attempt fair share for this task is exhausted "
            "(failed attempts also consume it until refunded as transport "
            "failures)."
        )
    return "The configured fetch-attempt budget has been exhausted."


def analyze_gaps(
    request: SearchRequest,
    evidence: list[EvidenceRecord],
    documents: list[ExtractedDocument],
    budget: BudgetSnapshot,
    *,
    pending_domains: list[str] | None = None,
) -> GapAnalysis:
    """Produce a deterministic baseline gap decision for the agent quality loop."""
    if pending_domains:
        return GapAnalysis(
            missing_dimensions=["domain approval"],
            next_queries=[],
            decision="approval_required",
            reason="Top-K candidate domains require approval before fetching.",
            budget=budget,
        )
    if budget.exhausted:
        reason = _budget_exhaustion_reason(budget)
        return GapAnalysis(
            covered_dimensions=[record.claim for record in evidence[:5]],
            missing_dimensions=[] if evidence else [request.objective],
            decision="budget_exhausted",
            reason=reason,
            budget=budget,
        )
    independent_domains = {urlsplit(item.source_url).hostname for item in evidence}
    if len(documents) >= 3 and len(independent_domains) >= 2 and evidence:
        return GapAnalysis(
            covered_dimensions=[record.claim for record in evidence[:5]],
            decision="complete",
            reason="At least three fetched documents and two independent domains produced evidence.",
            budget=budget,
        )
    return GapAnalysis(
        covered_dimensions=[record.claim for record in evidence[:3]],
        missing_dimensions=[request.objective],
        next_queries=request.queries[:2],
        decision="continue",
        reason="More successfully fetched, independently sourced evidence is required.",
        budget=budget,
    )


class WebResearchPipeline:
    """Coordinate one deterministic web research iteration."""

    def __init__(
        self,
        *,
        search: SearchAdapter,
        settings: WebPipelineSettings,
        reranker: RerankAdapter | None = None,
        approve: ApprovalAdapter | None = None,
        render_dynamic: DynamicRenderAdapter | None = None,
        external_extractors: list[ExternalExtractAdapter] | None = None,
        evidence_extractor: EvidenceAdapter | None = None,
        fetch_backends: dict[str, ExternalExtractAdapter] | None = None,
        backend_order: list[str] | None = None,
        allow_url: Callable[[str], bool] | None = None,
        progress: Callable | None = None,
        extract_evidence: bool = True,
        result_cache=None,
        evidence_context: dict | None = None,
    ) -> None:
        """Store provider adapters and bounded runtime settings."""
        self.search = search
        self.settings = settings
        self.reranker = reranker
        self.approve = approve
        self.render_dynamic = render_dynamic
        self.external_extractors = external_extractors or []
        self.evidence_extractor = evidence_extractor
        self.fetch_backends = dict(fetch_backends or {})
        if render_dynamic:
            self.fetch_backends["playwright"] = render_dynamic
        for index, extractor in enumerate(self.external_extractors):
            self.fetch_backends[f"external_{index}"] = extractor
        self.backend_order = (
            backend_order
            if backend_order is not None
            else ["local", *self.fetch_backends]
        )
        self.allow_url = allow_url
        self.progress = progress
        self.extract_evidence = extract_evidence
        self.result_cache = result_cache
        self.evidence_context = evidence_context or {}

    async def run(
        self,
        request: SearchRequest,
        *,
        remaining_fetches: int | None = None,
        on_physical_fetch: Callable[[], None] | None = None,
        fetch_budget_exhaustion_scope: Literal[
            "none", "task", "run", "run_and_task"
        ] = "none",
        run_id: str | None = None,
        fetch_budget_exhaustion_cause: Literal[
            "attempts", "transport_failures"
        ] = "attempts",
    ) -> WebResearchResult:
        """Run Search, select Top K, fetch, extract, and create citable evidence."""
        allowed_fetches = min(
            self.settings.fetch_top_k,
            self.settings.max_fetches
            if remaining_fetches is None
            else max(0, remaining_fetches),
        )
        if allowed_fetches == 0 and fetch_budget_exhaustion_scope != "none":
            # Zero allocation behind an authenticated budget wall: no
            # candidate can be fetched, so running the provider search would
            # only burn search quota (E2E round 9: 24 zero-quota iterations
            # each still issued Tavily calls). Return the deterministic
            # exhausted result directly.
            budget = BudgetSnapshot(
                search_calls=0,
                candidates=0,
                fetch_attempts=0,
                fetched_documents=0,
                reserved_fetches=0,
                max_fetches=self.settings.max_fetches,
                exhausted=True,
                exhaustion_scope=fetch_budget_exhaustion_scope,
                exhaustion_cause=fetch_budget_exhaustion_cause,
            )
            return WebResearchResult(
                request=request,
                candidates=[],
                ranked_candidates=[],
                provider_syntheses=[],
                approval_batch=DomainApprovalBatch(
                    run_id=run_id or "default",
                    iteration=request.iteration,
                    domains=[],
                    urls=[],
                ),
                fetches=[],
                documents=[],
                chunks=[],
                evidence=[],
                gap_analysis=analyze_gaps(request, [], [], budget),
                errors=[],
            )
        batch = await self.search(request)
        candidates = normalize_candidates(batch.candidates, request.candidate_limit)
        ranked = await rank_candidates(
            request.objective,
            candidates,
            reranker=self.reranker,
            top_k=allowed_fetches,
            min_authority=self.settings.min_source_authority,
            max_per_domain=allowed_fetches if self.settings.finite_corpus else 2,
        )
        selected = [item.candidate for item in ranked if item.selected]
        authority_rejected_all = bool(
            candidates
            and not selected
            and any(item.reason == "below_authority_threshold" for item in ranked)
        )
        approval = None
        if self.approve and selected:
            approval = await self.approve(selected, request.iteration)
        # The approval adapter is skipped when nothing was selected, so the
        # caller-supplied identity is the only non-"default" source for the
        # synthetic empty batch attached to the result.
        batch_run_id = (
            approval.run_id if approval is not None else (run_id or "default")
        )
        pending = set(approval.pending_domains if approval else [])
        denied = set(approval.denied_domains if approval else [])
        fetchable = [c for c in selected if c.domain not in pending | denied]
        for item in ranked:
            if item.selected and item.candidate.domain in pending | denied:
                item.selected = False
                item.reason = (
                    "domain_approval_pending"
                    if item.candidate.domain in pending
                    else "domain_denied"
                )
        # Fill denied slots from the ranked pool when the replacement's domain is
        # already allowed. New undecided domains are collected into the same
        # logical approval batch but are never fetched prematurely.
        if self.approve and len(fetchable) < allowed_fetches:
            replacement_domain_counts = Counter(item.domain for item in fetchable)
            for replacement in [
                item
                for item in ranked
                if not item.selected
                and item.authority >= self.settings.min_source_authority
            ]:
                if len(fetchable) >= allowed_fetches:
                    break
                candidate = replacement.candidate
                if (
                    candidate.domain in denied
                    or candidate in fetchable
                    or replacement_domain_counts[candidate.domain] >= 2
                ):
                    continue
                replacement_approval = await self.approve(
                    [candidate], request.iteration
                )
                if approval:
                    approval.domains = list(
                        dict.fromkeys(approval.domains + replacement_approval.domains)
                    )
                    approval.urls = list(
                        dict.fromkeys(approval.urls + replacement_approval.urls)
                    )
                    approval.pending_domains = list(
                        dict.fromkeys(
                            approval.pending_domains
                            + replacement_approval.pending_domains
                        )
                    )
                    approval.denied_domains = list(
                        dict.fromkeys(
                            approval.denied_domains
                            + replacement_approval.denied_domains
                        )
                    )
                pending.update(replacement_approval.pending_domains)
                denied.update(replacement_approval.denied_domains)
                if candidate.domain not in pending | denied:
                    replacement.selected = True
                    replacement.reason = "selected_replacement"
                    fetchable.append(candidate)
                    replacement_domain_counts[candidate.domain] += 1
        if pending and not fetchable:
            budget = BudgetSnapshot(
                search_calls=batch.search_calls or len(request.queries),
                candidates=len(candidates),
                reserved_fetches=allowed_fetches,
                max_fetches=self.settings.max_fetches,
            )
            gap = analyze_gaps(request, [], [], budget, pending_domains=sorted(pending))
            return WebResearchResult(
                request=request,
                candidates=candidates,
                ranked_candidates=ranked,
                provider_syntheses=batch.syntheses,
                provider_results=batch.provider_results,
                approval_batch=approval,
                gap_analysis=gap,
                errors=batch.errors,
            )

        semaphore = asyncio.Semaphore(self.settings.global_concurrency)
        host_semaphores: defaultdict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self.settings.per_host_concurrency)
        )

        async def redirect_allowed(target: str) -> bool:
            host = urlsplit(target).hostname or ""
            return bool(
                approval and host in approval.domains and host not in pending | denied
            )

        async def fetch_one(candidate: CandidateSource):
            from open_deep_research.agentscope_runtime.search_providers import (
                error_code,
                preserve_control_error,
            )

            cache_key = (self.settings.cache_namespace, candidate.canonical_url)
            lock = _FETCH_LOCKS.setdefault(cache_key, asyncio.Lock())
            async with lock:
                result = FetchResult(
                    candidate_id=candidate.candidate_id,
                    requested_url=candidate.canonical_url,
                )
                if self.allow_url and not self.allow_url(candidate.canonical_url):
                    result.failure_class = "source_scope_denied"
                    return result, None
                cached = cached_document(
                    self.settings.cache_namespace,
                    candidate.canonical_url,
                    evidence=self.extract_evidence,
                )
                from open_deep_research.agentscope_runtime.efficiency import fingerprint

                document_key = "document:" + fingerprint(candidate.canonical_url)
                if cached is None and self.result_cache is not None:
                    saved = await self.result_cache.get(document_key)
                    if saved:
                        restored = ExtractedDocument.model_validate(saved)
                        if not needs_extraction_fallback(restored, evidence=self.extract_evidence):
                            cached = restored
                            _DOCUMENT_CACHE[cache_key] = restored
                if cached is not None and (
                    self.allow_url is None or self.allow_url(cached.final_url)
                ):
                    if (
                        egress_authorizer.get() is not None
                        and await authorize_url(cached.final_url) != "allow"
                    ):
                        result.failure_class = "approval_required"
                        return result, None
                    if self.result_cache is not None:
                        await self.result_cache.progress({"counters": {"document_cache_hits": 1}})
                    return result.model_copy(
                        update={
                            "success": True,
                            "adapter": "run_cache",
                            "final_url": cached.final_url,
                            "content_type": cached.content_type,
                            "content_hash": cached.content_hash,
                        }
                    ), cached
                async with semaphore, host_semaphores[candidate.domain]:
                    if on_physical_fetch is not None:
                        on_physical_fetch()
                    if self.progress:
                        await self.progress(
                            "fetch_started", url=candidate.canonical_url
                        )
                    attempts = []
                    for backend in self.backend_order:
                        if self.progress:
                            await self.progress(
                                "fetch_backend",
                                url=candidate.canonical_url,
                                backend=backend,
                            )
                        document = None
                        try:
                            if backend == "local":
                                raw = await fetch_local(
                                    candidate,
                                    self.settings,
                                    redirect_allowed=redirect_allowed,
                                )
                                result = raw.result
                                if not result.success:
                                    failure = result.failure_class or "fetch_failed"
                                    attempts.append(
                                        {
                                            "backend": backend,
                                            "status": "failed",
                                            "error_code": failure,
                                        }
                                    )
                                    retryable = (
                                        failure in TRANSPORT_FAILURE_CLASSES
                                        or failure == "http_429"
                                        or failure.startswith("http_5")
                                    )
                                    if not retryable:
                                        break
                                    continue
                                document = extract_document(
                                    candidate, raw, self.settings
                                )
                            elif backend in self.fetch_backends:
                                document = await self.fetch_backends[backend](
                                    candidate.canonical_url
                                )
                            else:
                                attempts.append(
                                    {"backend": backend, "status": "unavailable"}
                                )
                                continue
                            if document is None or not document.markdown.strip():
                                attempts.append({"backend": backend, "status": "empty"})
                                continue
                            if self.allow_url and not self.allow_url(
                                document.final_url
                            ):
                                result.failure_class = "source_scope_denied"
                                attempts.append(
                                    {
                                        "backend": backend,
                                        "status": "denied",
                                        "error_code": result.failure_class,
                                    }
                                )
                                break
                            final_host = urlsplit(document.final_url).hostname
                            if final_host != candidate.domain:
                                permitted = (
                                    await authorize_url(document.final_url) == "allow"
                                    if egress_authorizer.get() is not None
                                    else await redirect_allowed(document.final_url)
                                )
                                if not permitted:
                                    result.failure_class = "approval_required"
                                    attempts.append(
                                        {
                                            "backend": backend,
                                            "status": "denied",
                                            "error_code": result.failure_class,
                                        }
                                    )
                                    break
                            if needs_extraction_fallback(
                                document, evidence=self.extract_evidence
                            ):
                                attempts.append(
                                    {
                                        "backend": backend,
                                        "status": "insufficient_content",
                                    }
                                )
                                continue
                            document = document.model_copy(
                                update={"candidate_id": candidate.candidate_id}
                            )
                            attempts.append({"backend": backend, "status": "completed"})
                            _DOCUMENT_CACHE[cache_key] = document
                            if (self.result_cache is not None
                                    and getattr(self.result_cache, "can_store_document", lambda _: True)(document.model_dump(mode="json"))
                                    and await self.result_cache.get(document_key) is None):
                                await self.result_cache.begin(document_key)
                                await self.result_cache.commit(document_key, document.model_dump(mode="json"))
                            result = result.model_copy(
                                update={
                                    "success": True,
                                    "adapter": backend,
                                    "final_url": document.final_url,
                                    "content_type": document.content_type,
                                    "content_hash": document.content_hash,
                                    "failure_class": None,
                                    "failure_message": None,
                                    "backend_attempts": attempts,
                                    "fetched_at": datetime.now(UTC),
                                }
                            )
                            if self.progress:
                                await self.progress(
                                    "fetch_completed",
                                    url=document.final_url,
                                    backend=backend,
                                )
                            return result, document
                        except PermissionError as exc:
                            result.failure_class = "source_or_network_denied"
                            attempts.append(
                                {
                                    "backend": backend,
                                    "status": "denied",
                                    "error_code": str(exc)[:120],
                                }
                            )
                            break
                        except Exception as exc:  # noqa: BLE001 - normalize external failures after preserving runtime control
                            preserve_control_error(exc)
                            code = error_code(exc)
                            attempts.append(
                                {
                                    "backend": backend,
                                    "status": "failed",
                                    "error_code": code,
                                }
                            )
                            if code in {
                                "authentication_failed",
                                "http_404",
                                "http_410",
                            }:
                                result.failure_class = code
                                break
                    result.success = False
                    result.backend_attempts = attempts
                    result.failure_class = (
                        result.failure_class or "no_extractor_produced_usable_content"
                    )
                    return result, None

        fetch_tasks = [
            asyncio.create_task(fetch_one(candidate)) for candidate in fetchable
        ]
        try:
            fetched = await asyncio.gather(*fetch_tasks)
        except BaseException:
            for task in fetch_tasks:
                task.cancel()
            await asyncio.gather(*fetch_tasks, return_exceptions=True)
            raise
        fetch_results = [item[0] for item in fetched]
        documents = [item[1] for item in fetched if item[1] is not None]
        transport_failed_fetches = sum(
            1
            for result in fetch_results
            if not result.success and result.failure_class in TRANSPORT_FAILURE_CLASSES
        )
        errors = list(batch.errors)
        errors.extend(
            f"{result.requested_url}: {result.failure_class}"
            for result in fetch_results
            if not result.success
        )
        if authority_rejected_all:
            errors.append("no_candidates_met_source_authority_threshold")

        # Content-hash dedupe after extraction.
        unique_documents = list({doc.content_hash: doc for doc in documents}.values())
        saved_progress = await self.result_cache.progress() if self.result_cache is not None else {}
        inspection_ids = sorted(r["requirement_id"] for r in self.evidence_context.get("requirements", [])
                                if r.get("kind", "factual") == "factual") or ["__default__"]
        document_progress = {}
        all_chunks: list[DocumentChunk] = []
        for document in unique_documents if self.extract_evidence else []:
            document_chunks = chunk_document(document, self.settings)
            previous = saved_progress.get("documents", {}).get(document.document_id, {})
            visited = set(previous.get("visited_chunks", []))
            document_progress[document.document_id] = {
                "url": document.final_url, "content_hash": document.content_hash,
                "total_chunks": len(document_chunks), "visited_chunks": sorted(visited),
                "processed_chunks": previous.get("processed_chunks", []),
                "blocked_chunks": previous.get("blocked_chunks", []),
                "inspections": {rid: dict(previous.get("inspections", {}).get(rid, {})) for rid in inspection_ids},
            }
            if self.result_cache is not None:
                inspections = document_progress[document.document_id]["inspections"]
                checked = set.intersection(*(set(row.get("processed_chunks", [])) | set(row.get("blocked_chunks", []))
                                             for row in inspections.values()))
                document_chunks = [chunk for chunk in document_chunks if chunk.chunk_id not in checked]
            all_chunks.extend(
                select_chunks(
                    request.objective,
                    document_chunks,
                    self.settings.max_chunks_per_document,
                )
            )
        selected_chunks = select_chunks(
            request.objective, all_chunks, self.settings.max_chunks_per_iteration
        )
        document_by_id = {
            document.document_id: document for document in unique_documents
        }
        deterministic_evidence = evidence_from_chunks(
            request.objective,
            document_by_id,
            selected_chunks,
        )
        model_checked = False
        if self.evidence_extractor and self.extract_evidence and selected_chunks:
            try:
                async def extract():
                    values = await self.evidence_extractor(
                        self.evidence_context.get("objective") or request.objective,
                        document_by_id, selected_chunks,
                    )
                    if getattr(self.evidence_extractor, "last_failure", False):
                        raise ValueError("evidence_extraction_unavailable")
                    return [value.model_dump(mode="json") for value in values]

                if self.result_cache is None:
                    model_evidence = [EvidenceRecord.model_validate(value) for value in await extract()]
                else:
                    values = await self.result_cache.compute("extraction", {
                        "version": 1, "context": self.evidence_context,
                        "documents": sorted(doc.content_hash for doc in unique_documents),
                        "chunks": sorted(chunk.chunk_id for chunk in selected_chunks),
                    }, extract)
                    model_evidence = [EvidenceRecord.model_validate(value) for value in values]
                model_checked = True
            except Exception as exc:  # deterministic content fallback only  # noqa: BLE001 - normalize external failures after preserving runtime control
                from open_deep_research.agentscope_runtime.search_providers import (
                    preserve_control_error,
                )

                preserve_control_error(exc)
                model_evidence = []
            evidence = merge_evidence_records(
                model_evidence,
                deterministic_evidence,
            )
        else:
            evidence = deterministic_evidence
        authority_by_candidate = {
            item.candidate.candidate_id: item.authority for item in ranked
        }
        candidate_by_document = {
            document.document_id: document.candidate_id for document in unique_documents
        }
        evidence = [
            record.model_copy(
                update={
                    "source_authority": authority_by_candidate.get(
                        candidate_by_document.get(record.document_id, ""),
                        0.0,
                    )
                }
            )
            for record in evidence
        ]
        if self.result_cache is not None and self.extract_evidence:
            for chunk in selected_chunks:
                item = document_progress[chunk.document_id]
                item["visited_chunks"].append(chunk.chunk_id)
                if inspect_untrusted_content(chunk.text):
                    item["blocked_chunks"].append(chunk.chunk_id)
                elif model_checked:
                    item["processed_chunks"].append(chunk.chunk_id)
                for inspection in item["inspections"].values():
                    for field in ("visited_chunks", "processed_chunks", "blocked_chunks"):
                        if chunk.chunk_id in item[field]:
                            inspection[field] = sorted(set(inspection.get(field, [])) | {chunk.chunk_id})
            for item in document_progress.values():
                for key in ("visited_chunks", "processed_chunks", "blocked_chunks"):
                    item[key] = sorted(set(item[key]))
                item["inspection_complete"] = len(item["processed_chunks"]) >= item["total_chunks"]
            updated = await self.result_cache.progress({
                "documents": document_progress,
                "candidates": {record.evidence_id: {**record.model_dump(mode="json"), "requirement_ids": inspection_ids} for record in evidence},
            })
            evidence = [EvidenceRecord.model_validate(value) for value in updated.get("candidates", {}).values()
                        if value.get("document_id") in document_by_id
                        and (self.allow_url is None or self.allow_url(value["source_url"]))]
            if self.progress:
                await self.progress("inspection_updated", metrics={
                    "processed_chunks": sum(len(v["processed_chunks"]) for v in document_progress.values()),
                    "total_chunks": sum(v["total_chunks"] for v in document_progress.values()),
                    "evidence_count": len(evidence),
                })
        budget = BudgetSnapshot(
            search_calls=batch.search_calls or len(request.queries),
            candidates=len(candidates),
            fetch_attempts=sum(
                result.adapter != "run_cache" for result in fetch_results
            ),
            fetched_documents=len(unique_documents),
            reserved_fetches=allowed_fetches,
            max_fetches=self.settings.max_fetches,
            exhausted=len(unique_documents) >= self.settings.max_fetches
            or allowed_fetches == 0,
            exhaustion_scope=(
                fetch_budget_exhaustion_scope if allowed_fetches == 0 else "none"
            ),
            transport_failed_fetches=transport_failed_fetches,
            exhaustion_cause=(
                fetch_budget_exhaustion_cause if allowed_fetches == 0 else "none"
            ),
        )
        gap = analyze_gaps(
            request,
            evidence,
            unique_documents,
            budget,
            pending_domains=sorted(pending) or None,
        )
        return WebResearchResult(
            request=request,
            candidates=candidates,
            ranked_candidates=ranked,
            provider_syntheses=batch.syntheses,
            provider_results=batch.provider_results,
            approval_batch=approval
            or DomainApprovalBatch(
                run_id=batch_run_id,
                iteration=request.iteration,
                domains=sorted({candidate.domain for candidate in selected}),
                urls=[candidate.canonical_url for candidate in selected],
            ),
            fetches=fetch_results,
            documents=unique_documents,
            chunks=selected_chunks,
            evidence=evidence,
            gap_analysis=gap,
            errors=errors,
        )


__all__ = [
    "COMPLETE_SENTENCE_RE",
    "RawFetch",
    "WebPipelineSettings",
    "WebResearchPipeline",
    "cached_document",
    "canonicalize_url",
    "chunk_document",
    "clear_run_web_cache",
    "evidence_from_chunks",
    "extract_document",
    "extract_html",
    "extract_pdf",
    "fetch_local",
    "merge_evidence_records",
    "normalize_candidates",
    "rank_candidates",
    "stable_id",
]
