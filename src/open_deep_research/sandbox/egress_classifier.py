"""Model-based egress approval classifier for sandboxed research runs.

The classifier mirrors the Claude Code auto-mode transcript classifier,
adapted to domain-level egress decisions:

- Two-stage pipeline: a fast single-word filter first; a structured,
  chain-of-thought review only for targets the fast stage did not clear.
- Reasoning-blind input: only the target host, the requesting tool, and a
  short user-intent anchor are visible. Tool outputs and page contents
  never enter the prompt (structural prompt-injection defense).
- Fail-safe direction: any timeout, transport error, budget exhaustion, or
  repeated failure degrades to ``ask`` (manual approval), never to allow.

The classifier talks to models only through the injected
``EgressModelInvoker`` and persists through the ``EgressLedger``; the
Gateway data plane owns orchestration, approval reuse, and the invoker
implementation (see ``sandbox.gateway``).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

EgressVerdict = Literal["allow", "ask", "deny"]


class _ClassifierBudgetExhausted(Exception):
    """No model call remains for this target."""

_EGRESS_VERDICTS: frozenset[str] = frozenset({"allow", "ask", "deny"})

# Short user-intent anchor cap; keeps prompts bounded even if a caller
# passes an oversized brief.
_MAX_INTENT_CHARS = 500

# Common two-label public suffixes where the registrable name needs the
# third label (``example.co.uk`` -> ``co.uk`` is the suffix, not the site).
_TWO_LABEL_PUBLIC_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk",
        "org.uk",
        "gov.uk",
        "ac.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.jp",
        "or.jp",
        "ne.jp",
        "co.cn",
        "com.cn",
        "net.cn",
        "org.cn",
        "com.br",
        "com.mx",
        "co.in",
        "co.kr",
        "com.sg",
        "com.hk",
        "co.nz",
        "com.tr",
        "com.ar",
    }
)


def registered_domain(host: str) -> str:
    """Approximate the registrable domain of a normalized hostname.

    Uses a small two-label public-suffix list; unknown multi-part TLDs fall
    back to the last two labels. This value is display/prompt context only;
    authorization and cache reuse use the exact host, port, capability and intent.
    """
    labels = [label for label in host.lower().strip().rstrip(".").split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    last_two = ".".join(labels[-2:])
    if last_two in _TWO_LABEL_PUBLIC_SUFFIXES:
        return ".".join(labels[-3:])
    return last_two


class EgressDomainVerdict(BaseModel):
    """Structured classifier verdict for one network target."""

    model_config = ConfigDict(extra="forbid")

    verdict: EgressVerdict
    category: str = Field(
        default="unknown",
        max_length=64,
        description="Reputation category, e.g. official_docs, academic, news, vendor, forum, unknown, suspicious.",
    )
    risk_tags: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Short reputation markers such as parked_domain, mimics_brand, file_sharing.",
    )
    reason: str = Field(default="", max_length=500)


class EgressModelCall(BaseModel):
    """One backend-agnostic classifier model call."""

    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]]
    max_output_tokens: int = Field(ge=1)
    temperature: float = 0.0
    structured_schema: dict[str, Any] | None = None
    logical_operation_id: str = Field(min_length=1)


class EgressModelReply(BaseModel):
    """Normalized outcome of one classifier model call."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    content: str | None = None
    structured: dict[str, Any] | None = None
    served_model: str | None = None


class EgressModelInvoker(Protocol):
    """Dispatches one classifier call through Gateway model machinery."""

    async def __call__(self, call: EgressModelCall) -> EgressModelReply:
        """Execute one call and return its normalized reply."""
        ...


@dataclass(frozen=True)
class EgressClassificationEntry:
    """One durable classification record (run-scoped audit trail)."""

    fingerprint: str
    registered_domain: str
    host: str
    port: int
    verdict: EgressVerdict
    source: Literal["stage1", "stage2", "human_allow", "human_deny"]
    capability: str = "tool.egress"
    intent_hash: str = ""
    version: int = 2
    tool: str = ""
    category: str = "unknown"
    risk_tags: tuple[str, ...] = ()
    reason: str = ""
    model: str | None = None
    classified_at: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        """Serialize for the ledger transport."""
        return {
            "fingerprint": self.fingerprint,
            "capability": self.capability,
            "intent_hash": self.intent_hash,
            "version": self.version,
            "registered_domain": self.registered_domain,
            "host": self.host,
            "port": self.port,
            "verdict": self.verdict,
            "source": self.source,
            "tool": self.tool,
            "category": self.category,
            "risk_tags": list(self.risk_tags),
            "reason": self.reason[:500],
            "model": self.model,
            "classified_at": self.classified_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EgressClassificationEntry:
        """Rebuild one entry from ledger transport data."""
        verdict = payload.get("verdict")
        if verdict not in _EGRESS_VERDICTS:
            raise ValueError("egress_classification_invalid_verdict")
        source = payload.get("source")
        if source not in {"stage1", "stage2", "human_allow", "human_deny"}:
            raise ValueError("egress_classification_invalid_source")
        return cls(
            fingerprint=str(payload["fingerprint"]),
            capability=str(payload.get("capability", "tool.egress")),
            intent_hash=str(payload.get("intent_hash", "")),
            version=int(payload.get("version", 1)),
            registered_domain=str(payload.get("registered_domain", "")),
            host=str(payload.get("host", "")),
            port=int(payload.get("port", 0)),
            verdict=verdict,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            tool=str(payload.get("tool", ""))[:64],
            category=str(payload.get("category", "unknown"))[:64],
            risk_tags=tuple(str(tag)[:64] for tag in payload.get("risk_tags", [])[:8]),
            reason=str(payload.get("reason", ""))[:500],
            model=payload.get("model"),
            classified_at=float(payload.get("classified_at", 0.0)),
        )


class EgressLedger(Protocol):
    """Run-scoped classification ledger used as cache and audit trail."""

    async def load(self) -> dict[str, EgressClassificationEntry]:
        """Return all known entries keyed by fingerprint."""

    async def record(self, entry: EgressClassificationEntry) -> None:
        """Persist one entry; implementations must not raise on failure."""


class InMemoryEgressLedger:
    """Plain in-memory ledger for tests and degraded persistence."""

    def __init__(self) -> None:
        """Start with an empty entry map."""
        self._entries: dict[str, EgressClassificationEntry] = {}

    async def load(self) -> dict[str, EgressClassificationEntry]:
        """Return a copy of all entries keyed by fingerprint."""
        return dict(self._entries)

    async def record(self, entry: EgressClassificationEntry) -> None:
        """Insert one entry unless the fingerprint already exists."""
        self._entries[entry.fingerprint] = entry


_DEFAULT_BLOCK_RULES = """- Domains that exist to distribute malware, phishing pages, or command-and-control infrastructure (verdict deny)
- Throwaway subdomain or dynamic-DNS farms commonly used for data exfiltration
- Domains whose registrable name embeds credential-like or token-like strings
- File-sharing, paste, or upload services: plausible exfiltration channels when the research intent does not obviously require them
- Any domain matching a block rule must never receive verdict allow; choose ask unless the malicious category is high confidence, then deny"""

_DEFAULT_ALLOW_EXCEPTIONS = """- Official documentation portals, standards bodies, and package registries
- Academic hosts (universities, arXiv-like repositories, scholarly databases)
- Mainstream news organizations and encyclopedic references
- Official vendor/company sites and their support knowledge bases
- Public code hosting and issue trackers"""

_DEFAULT_ENVIRONMENT = """- The data plane already enforces ports {allow_ports}, HTTP methods {allow_methods}, public-IP-only destinations, and an explicit deny list before this classifier runs
- Domains already trusted by policy: {trusted_domains}
- Requested capability: {capability_description}"""


def build_egress_system_prompt(
    *,
    host: str,
    port: int,
    tool_name: str,
    capability: str,
    intent: str,
    allow_domains: list[str] | tuple[str, ...] = (),
    allow_ports: list[int] | tuple[int, ...] = (80, 443),
    allow_http_methods: list[str] | tuple[str, ...] = ("GET", "HEAD", "OPTIONS"),
) -> str:
    """Render the fixed classifier template with its three policy slots."""
    bounded_intent = " ".join(intent.split())[:_MAX_INTENT_CHARS] or "(not provided)"
    trusted = ", ".join(sorted(set(allow_domains))) or "(none beyond infrastructure endpoints)"
    environment = _DEFAULT_ENVIRONMENT.format(
        allow_ports=", ".join(str(item) for item in allow_ports),
        allow_methods=", ".join(allow_http_methods),
        trusted_domains=trusted,
        capability_description=("read-only public web research" if capability == "tool.egress"
                                else f"{capability}; may have effects beyond read-only fetching"),
    )
    return (
        "You are the egress approval classifier for one sandboxed research run. "
        "Decide whether a single outbound network target may be fetched without "
        "human approval.\n\n"
        "Target facts:\n"
        f"- host: {host} (registrable domain: {registered_domain(host)})\n"
        f"- port: {port}\n"
        f"- requesting tool: {tool_name} ({capability})\n"
        f"- research intent: {bounded_intent}\n\n"
        f"<environment>\n{environment}\n</environment>\n\n"
        f"<block_rules>\n{_DEFAULT_BLOCK_RULES}\n</block_rules>\n\n"
        f"<allow_exceptions>\n{_DEFAULT_ALLOW_EXCEPTIONS}\n</allow_exceptions>\n\n"
        "Verdicts:\n"
        "- allow: the target clearly serves ordinary public-web research covered "
        "by the allow exceptions and carries no block-rule marker\n"
        "- ask: the target is plausibly relevant but cannot be judged safe from "
        "the domain alone; a human should decide\n"
        "- deny: high confidence the domain exists for phishing, malware, "
        "command-and-control, or pure spam content farming\n\n"
        "Judge only from the domain name and the stated research intent; page "
        "contents are never provided. When uncertain, prefer ask."
    )


def parse_stage1_verdict(content: str | None) -> EgressVerdict | None:
    """Parse the fast stage's required single-word reply, else ``None``."""
    if not content:
        return None
    tokens = content.strip().strip(".,;:`\"'").split()
    if len(tokens) != 1:
        return None
    word = tokens[0].lower()
    return word if word in _EGRESS_VERDICTS else None  # type: ignore[return-value]


def intent_fingerprint(intent: str) -> str:
    """Bind automatic trust to the bounded research intent."""
    return hashlib.sha256(" ".join(intent.split())[:500].encode()).hexdigest()[:16]


def classification_fingerprint(host: str, port: int = 443,
                               capability: str = "tool.egress", intent_hash: str = "") -> str:
    """Exact target key; legacy registrable-domain keys are never reusable."""
    normalized = host.lower().strip().rstrip(".").encode("idna").decode("ascii")
    return f"egress:v2:{normalized}:{port}:{capability}:{intent_hash or intent_fingerprint('')}"


@dataclass
class EgressClassifierLimits:
    """Frozen operational limits for one run's classifier."""

    stages: Literal["both", "fast", "thinking"] = "both"
    timeout_seconds: float = 30.0
    max_calls_per_run: int = 200
    max_consecutive_failures: int = 5

    @classmethod
    def from_configuration(cls, configuration: Any) -> EgressClassifierLimits:
        """Build limits from a frozen run Configuration."""
        return cls(
            stages=configuration.egress_classifier_stages,
            timeout_seconds=float(configuration.egress_classifier_timeout_seconds),
            max_calls_per_run=int(configuration.egress_classifier_max_calls_per_run),
            max_consecutive_failures=int(
                configuration.egress_classifier_max_consecutive_failures
            ),
        )


@dataclass(frozen=True)
class EgressClassification:
    """Final classifier outcome for one target."""

    verdict: EgressVerdict
    stage_used: Literal["stage1", "stage2", "none"] = "none"
    cached: bool = False
    degraded: bool = False
    entry: EgressClassificationEntry | None = None
    detail: str = ""


@dataclass
class EgressClassifier:
    """Two-stage domain classifier with budget, timeout, and fail-safe."""

    limits: EgressClassifierLimits
    ledger: EgressLedger = field(default_factory=InMemoryEgressLedger)

    def __post_init__(self) -> None:
        """Initialize per-run counters, cache, and fingerprint locks."""
        self._state_revision = 0
        self._calls_used = 0
        self._consecutive_failures = 0
        self._degraded = False
        self._entries: dict[str, EgressClassificationEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def warm(self) -> None:
        """Load persisted entries into the in-memory cache (best effort)."""
        try:
            self._entries = {key: entry for key, entry in (await self.ledger.load()).items()
                             if entry.version >= 2 and key.startswith("egress:v2:")
                             and entry.source in {"stage1", "stage2"}}
            if hasattr(self.ledger, "load_state"):
                state = await self.ledger.load_state()
                self._state_revision = int(state.get("revision", 0))
                self._calls_used = int(state.get("calls_used", 0))
                self._consecutive_failures = int(state.get("consecutive_failures", 0))
                self._degraded = bool(state.get("degraded", False))
        except Exception:  # noqa: BLE001 - cache warm failure must not break decisions
            self._entries = {}
            self._degraded = True

    def lookup(self, host: str, port: int = 443, capability: str = "tool.egress",
               intent: str = "") -> EgressClassification | None:
        """Return a cached verdict for an exact target and research intent."""
        entry = self._entries.get(classification_fingerprint(host, port, capability, intent_fingerprint(intent)))
        if entry is None:
            return None
        return EgressClassification(
            verdict=entry.verdict,
            cached=True,
            entry=entry,
            detail="ledger",
        )

    async def record_human_decision(
        self,
        *,
        host: str,
        port: int,
        allowed: bool,
        tool: str = "",
    ) -> EgressClassificationEntry:
        """Record a legacy audit projection without creating reusable permission."""
        registered = registered_domain(host)
        entry = EgressClassificationEntry(
            fingerprint=classification_fingerprint(host, port),
            registered_domain=registered,
            host=host,
            port=port,
            verdict="allow" if allowed else "deny",
            source="human_allow" if allowed else "human_deny",
            tool=tool,
            reason="human decision via security approval",
            classified_at=time.time(),
        )
        # Human decisions are authoritative in SecurityApprovalStore, never in this cache.
        await self._persist(entry)
        return entry

    async def classify_target(
        self,
        *,
        host: str,
        port: int,
        tool_name: str,
        capability: str,
        intent: str = "",
        invoker: EgressModelInvoker,
        allow_domains: list[str] | tuple[str, ...] = (),
        allow_ports: list[int] | tuple[int, ...] = (80, 443),
        allow_http_methods: list[str] | tuple[str, ...] = ("GET", "HEAD", "OPTIONS"),
    ) -> EgressClassification:
        """Classify one target, fail-safe to ``ask`` on any failure."""
        registered = registered_domain(host)
        key = classification_fingerprint(host, port, capability, intent_fingerprint(intent))
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._entries.get(key)
            if cached is not None:
                return EgressClassification(
                    verdict=cached.verdict,
                    cached=True,
                    entry=cached,
                    detail="ledger",
                )
            if self._degraded:
                return EgressClassification(
                    verdict="ask",
                    degraded=True,
                    detail="classifier_degraded",
                )
            if self._calls_used >= self.limits.max_calls_per_run:
                return EgressClassification(
                    verdict="ask",
                    detail="classifier_budget_exhausted",
                )
            system_prompt = build_egress_system_prompt(
                host=host,
                port=port,
                tool_name=tool_name,
                capability=capability,
                intent=intent,
                allow_domains=allow_domains,
                allow_ports=allow_ports,
                allow_http_methods=allow_http_methods,
            )
            try:
                result = await asyncio.wait_for(
                    self._classify_locked(
                        host=host,
                        port=port,
                        registered=registered,
                        fingerprint=key,
                        tool_name=tool_name,
                        system_prompt=system_prompt,
                        capability=capability,
                        intent_hash=intent_fingerprint(intent),
                        invoker=invoker,
                    ),
                    timeout=self.limits.timeout_seconds,
                )
                # Count complete target classifications, not successful fast
                # screens preceding a failed structured review.
                if result.entry is not None:
                    self._consecutive_failures = 0
                elif result.detail == "stage1_unparseable":
                    self._register_failure()
                return result
            except _ClassifierBudgetExhausted:
                return EgressClassification(
                    verdict="ask", detail="classifier_budget_exhausted"
                )
            except asyncio.TimeoutError:
                self._register_failure()
                return EgressClassification(verdict="ask", detail="timeout")
            except Exception:  # noqa: BLE001 - fail-safe direction is always ask
                self._register_failure()
                return EgressClassification(verdict="ask", detail="error")
            finally:
                await self._save_state()

    async def _classify_locked(
        self,
        *,
        host: str,
        port: int,
        registered: str,
        fingerprint: str,
        tool_name: str,
        system_prompt: str,
        capability: str,
        intent_hash: str,
        invoker: EgressModelInvoker,
    ) -> EgressClassification:
        if self.limits.stages in {"both", "fast"}:
            stage1, model = await self._run_stage1(system_prompt, fingerprint, invoker)
            if stage1 == "allow":
                entry = await self._record(
                    host=host,
                    port=port,
                    registered=registered,
                    fingerprint=fingerprint,
                    capability=capability, intent_hash=intent_hash,
                    verdict="allow",
                    source="stage1",
                    tool=tool_name,
                    category="cleared_by_fast_filter",
                    model=model,
                )
                return EgressClassification(
                    verdict="allow", stage_used="stage1", entry=entry
                )
            if self.limits.stages == "fast":
                if stage1 is None:
                    return EgressClassification(
                        verdict="ask", detail="stage1_unparseable"
                    )
                entry = await self._record(
                    host=host,
                    port=port,
                    registered=registered,
                    fingerprint=fingerprint,
                    capability=capability, intent_hash=intent_hash,
                    verdict=stage1,
                    source="stage1",
                    tool=tool_name,
                    model=model,
                )
                return EgressClassification(
                    verdict=stage1, stage_used="stage1", entry=entry
                )
        # Stage 1 may have spent the last available call, including while
        # other domains were being classified concurrently.
        if self._calls_used >= self.limits.max_calls_per_run:
            return EgressClassification(
                verdict="ask", detail="classifier_budget_exhausted"
            )
        verdict, model = await self._run_stage2(system_prompt, fingerprint, invoker)
        entry = await self._record(
            host=host,
            port=port,
            registered=registered,
            fingerprint=fingerprint,
            capability=capability, intent_hash=intent_hash,
            verdict=verdict.verdict,
            source="stage2",
            tool=tool_name,
            category=verdict.category,
            risk_tags=tuple(verdict.risk_tags),
            reason=verdict.reason,
            model=model,
        )
        return EgressClassification(
            verdict=verdict.verdict,
            stage_used="stage2",
            entry=entry,
        )

    async def _run_stage1(
        self,
        system_prompt: str,
        fingerprint: str,
        invoker: EgressModelInvoker,
    ) -> tuple[EgressVerdict | None, str | None]:
        if self._calls_used >= self.limits.max_calls_per_run:
            raise _ClassifierBudgetExhausted
        self._calls_used += 1
        await self._save_state()
        reply = await invoker(
            EgressModelCall(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": (
                            "Fast screen. Reply with exactly one word: "
                            "allow, ask, or deny."
                        ),
                    },
                ],
                max_output_tokens=16,
                logical_operation_id=f"{fingerprint}:stage1:{self._calls_used}",
            )
        )
        if reply.status != "completed":
            raise RuntimeError("egress_classifier_stage1_failed")
        return parse_stage1_verdict(reply.content), reply.served_model

    async def _run_stage2(
        self,
        system_prompt: str,
        fingerprint: str,
        invoker: EgressModelInvoker,
    ) -> tuple[EgressDomainVerdict, str | None]:
        self._calls_used += 1
        await self._save_state()
        reply = await invoker(
            EgressModelCall(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": (
                            "Reconsider carefully. Reason step by step about the "
                            "registrable domain, its reputation category, and its "
                            "relevance to the research intent. Then report the "
                            "structured verdict."
                        ),
                    },
                ],
                max_output_tokens=1024,
                structured_schema=EgressDomainVerdict.model_json_schema(),
                logical_operation_id=f"{fingerprint}:stage2:{self._calls_used}",
            )
        )
        if reply.status != "completed":
            raise RuntimeError("egress_classifier_stage2_failed")
        if not reply.structured:
            raise RuntimeError("egress_classifier_stage2_missing_payload")
        verdict = EgressDomainVerdict.model_validate(reply.structured)
        return verdict, reply.served_model

    async def _record(
        self,
        *,
        host: str,
        port: int,
        registered: str,
        fingerprint: str,
        capability: str,
        intent_hash: str,
        verdict: EgressVerdict,
        source: Literal["stage1", "stage2"],
        tool: str = "",
        category: str = "unknown",
        risk_tags: tuple[str, ...] = (),
        reason: str = "",
        model: str | None = None,
    ) -> EgressClassificationEntry:
        entry = EgressClassificationEntry(
            fingerprint=fingerprint,
            capability=capability, intent_hash=intent_hash,
            registered_domain=registered,
            host=host,
            port=port,
            verdict=verdict,
            source=source,
            tool=tool,
            category=category,
            risk_tags=risk_tags,
            reason=reason,
            model=model,
            classified_at=time.time(),
        )
        self._entries[fingerprint] = entry
        await self._persist(entry)
        return entry

    async def _persist(self, entry: EgressClassificationEntry) -> None:
        try:
            await self.ledger.record(entry)
        except Exception:  # noqa: BLE001 - persistence is best-effort caching
            pass

    async def _save_state(self) -> None:
        if hasattr(self.ledger, "save_state"):
            self._state_revision += 1
            await self.ledger.save_state({
                "revision": self._state_revision,
                "calls_used": self._calls_used,
                "consecutive_failures": self._consecutive_failures,
                "degraded": self._degraded,
                "reason": "consecutive_failures" if self._degraded else (
                    "budget_exhausted" if self._calls_used >= self.limits.max_calls_per_run else ""),
            })

    def _register_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.limits.max_consecutive_failures:
            self._degraded = True

    @property
    def calls_used(self) -> int:
        """Model calls consumed so far for this run."""
        return self._calls_used

    @property
    def degraded(self) -> bool:
        """Whether consecutive failures disabled auto classification."""
        return self._degraded
