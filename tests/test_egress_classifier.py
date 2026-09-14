"""Unit tests for the egress approval classifier core."""

import asyncio

import pytest

from open_deep_research.sandbox.egress_classifier import (
    EgressClassificationEntry,
    EgressClassifier,
    EgressClassifierLimits,
    EgressModelCall,
    EgressModelReply,
    InMemoryEgressLedger,
    build_egress_system_prompt,
    classification_fingerprint,
    parse_stage1_verdict,
    registered_domain,
)


class FakeInvoker:
    """Scriptable invoker recording calls and replying per stage."""

    def __init__(
        self,
        *,
        stage1: str | None = "allow",
        stage2: dict | None = None,
        fail: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.stage1 = stage1
        self.stage2 = stage2 or {"verdict": "ask", "category": "unknown"}
        self.fail = fail
        self.delay = delay
        self.calls: list[EgressModelCall] = []

    async def __call__(self, call: EgressModelCall) -> EgressModelReply:
        self.calls.append(call)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            return EgressModelReply(status="failed")
        if call.structured_schema is None:
            return EgressModelReply(status="completed", content=self.stage1)
        return EgressModelReply(status="completed", structured=self.stage2)


class TestRegisteredDomain:
    def test_simple_two_label(self):
        assert registered_domain("example.com") == "example.com"

    def test_subdomain_collapses(self):
        assert registered_domain("docs.example.com") == "example.com"

    def test_two_label_public_suffix_takes_three(self):
        assert registered_domain("www.bbc.co.uk") == "bbc.co.uk"

    def test_single_label_passthrough(self):
        assert registered_domain("localhost") == "localhost"


class TestStage1Parsing:
    def test_plain_word(self):
        assert parse_stage1_verdict("allow") == "allow"

    def test_case_and_punctuation(self):
        assert parse_stage1_verdict("  DENY. ") == "deny"

    def test_multi_word_rejected(self):
        assert parse_stage1_verdict("allow this") is None

    def test_garbage_rejected(self):
        assert parse_stage1_verdict("banana") is None

    def test_empty_rejected(self):
        assert parse_stage1_verdict(None) is None


class TestSystemPrompt:
    def test_contains_target_and_slots(self):
        prompt = build_egress_system_prompt(
            host="docs.example.com",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            intent="研究 Rust 异步运行时",
            allow_domains=["trusted.internal"],
        )
        assert "docs.example.com" in prompt
        assert "example.com" in prompt
        assert "trusted.internal" in prompt
        assert "fetch_url" in prompt
        assert "<block_rules>" in prompt
        assert "<allow_exceptions>" in prompt
        assert "<environment>" in prompt

    def test_intent_bounded(self):
        prompt = build_egress_system_prompt(
            host="a.com",
            port=443,
            tool_name="t",
            capability="c",
            intent="x" * 5000,
        )
        assert "x" * 501 not in prompt


class TestClassifyTwoStage:
    @pytest.mark.asyncio
    async def test_stage1_allow_short_circuits(self):
        classifier = EgressClassifier(EgressClassifierLimits())
        invoker = FakeInvoker(stage1="allow")
        result = await classifier.classify_target(
            host="docs.python.org",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=invoker,
        )
        assert result.verdict == "allow"
        assert result.stage_used == "stage1"
        assert len(invoker.calls) == 1
        assert invoker.calls[0].max_output_tokens <= 16

    @pytest.mark.asyncio
    async def test_stage1_ask_escalates_to_stage2(self):
        classifier = EgressClassifier(EgressClassifierLimits())
        invoker = FakeInvoker(
            stage1="ask",
            stage2={"verdict": "allow", "category": "official_docs"},
        )
        result = await classifier.classify_target(
            host="kernel.org",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=invoker,
        )
        assert result.verdict == "allow"
        assert result.stage_used == "stage2"
        assert len(invoker.calls) == 2
        assert invoker.calls[1].structured_schema is not None

    @pytest.mark.asyncio
    async def test_stage1_unparseable_escalates_to_stage2(self):
        classifier = EgressClassifier(EgressClassifierLimits())
        invoker = FakeInvoker(
            stage1="probably fine",
            stage2={"verdict": "deny", "category": "suspicious"},
        )
        result = await classifier.classify_target(
            host="weird.example",
            port=443,
            tool_name="web_research",
            capability="proxy.connect",
            invoker=invoker,
        )
        assert result.verdict == "deny"
        assert result.stage_used == "stage2"

    @pytest.mark.asyncio
    async def test_fast_mode_never_calls_stage2(self):
        classifier = EgressClassifier(EgressClassifierLimits(stages="fast"))
        invoker = FakeInvoker(stage1="deny")
        result = await classifier.classify_target(
            host="evil.example",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=invoker,
        )
        assert result.verdict == "deny"
        assert len(invoker.calls) == 1

    @pytest.mark.asyncio
    async def test_fast_mode_unparseable_fails_safe_to_ask(self):
        classifier = EgressClassifier(EgressClassifierLimits(stages="fast"))
        invoker = FakeInvoker(stage1="no idea")
        result = await classifier.classify_target(
            host="anything.example",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=invoker,
        )
        assert result.verdict == "ask"

    @pytest.mark.asyncio
    async def test_thinking_mode_skips_stage1(self):
        classifier = EgressClassifier(EgressClassifierLimits(stages="thinking"))
        invoker = FakeInvoker(
            stage1="allow",
            stage2={"verdict": "ask", "category": "unknown"},
        )
        result = await classifier.classify_target(
            host="portal.example.com",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=invoker,
        )
        assert result.verdict == "ask"
        assert result.stage_used == "stage2"
        assert len(invoker.calls) == 1
        assert invoker.calls[0].structured_schema is not None


class TestLedgerCaching:
    @pytest.mark.asyncio
    async def test_same_registrable_domain_does_not_share_verdict(self):
        classifier = EgressClassifier(EgressClassifierLimits())
        invoker = FakeInvoker(stage1="allow")
        first = await classifier.classify_target(
            host="docs.example.com",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=invoker,
        )
        second = await classifier.classify_target(
            host="www.example.com",
            port=80,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=invoker,
        )
        assert first.cached is False
        assert second.cached is False
        assert second.verdict == "allow"
        assert len(invoker.calls) == 2

    @pytest.mark.asyncio
    async def test_warm_loads_persisted_entries(self):
        ledger = InMemoryEgressLedger()
        classifier = EgressClassifier(EgressClassifierLimits(), ledger=ledger)
        await classifier.classify_target(
            host="docs.example.com",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=FakeInvoker(stage1="allow"),
        )
        revived = EgressClassifier(EgressClassifierLimits(), ledger=ledger)
        await revived.warm()
        cached = revived.lookup("docs.example.com")
        assert cached is not None and cached.verdict == "allow"

    @pytest.mark.asyncio
    async def test_human_decision_is_audit_only(self):
        classifier = EgressClassifier(EgressClassifierLimits())
        await classifier.record_human_decision(
            host="portal.example.com", port=443, allowed=False
        )
        cached = classifier.lookup("other.example.com")
        assert cached is None
        assert classifier.lookup("portal.example.com") is None


class TestFailSafe:
    @pytest.mark.asyncio
    async def test_successful_fast_screen_does_not_reset_stage2_failure_streak(self):
        classifier = EgressClassifier(EgressClassifierLimits(max_consecutive_failures=2))
        invoker = FakeInvoker(stage1="ask", stage2={"verdict": "invalid"})
        for host in ("one.example", "two.example"):
            result = await classifier.classify_target(
                host=host, port=443, tool_name="fetch_url",
                capability="tool.egress", invoker=invoker,
            )
            assert result.verdict == "ask"
        assert classifier.degraded
        assert classifier.calls_used == 4

    @pytest.mark.asyncio
    @pytest.mark.parametrize("concurrent", [False, True])
    async def test_both_stages_obey_remaining_model_call_budget(self, concurrent):
        classifier = EgressClassifier(EgressClassifierLimits(max_calls_per_run=1))
        invoker = FakeInvoker(stage1="ask")
        results = await asyncio.gather(*[
            classifier.classify_target(host=host, port=443, tool_name="fetch_url",
                capability="tool.egress", invoker=invoker)
            for host in (["one.example", "two.example"] if concurrent else ["one.example"])
        ])
        assert len(invoker.calls) == classifier.calls_used == 1
        assert all(r.detail == "classifier_budget_exhausted" for r in results)
        assert not classifier.degraded

    @pytest.mark.asyncio
    async def test_transport_failure_fails_safe_to_ask(self):
        classifier = EgressClassifier(EgressClassifierLimits())
        result = await classifier.classify_target(
            host="down.example.com",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=FakeInvoker(fail=True),
        )
        assert result.verdict == "ask"
        assert result.detail == "error"

    @pytest.mark.asyncio
    async def test_timeout_fails_safe_to_ask(self):
        classifier = EgressClassifier(
            EgressClassifierLimits(timeout_seconds=0.05)
        )
        result = await classifier.classify_target(
            host="slow.example.com",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=FakeInvoker(delay=0.5),
        )
        assert result.verdict == "ask"
        assert result.detail == "timeout"

    @pytest.mark.asyncio
    async def test_consecutive_failures_degrade_classifier(self):
        limits = EgressClassifierLimits(max_consecutive_failures=2)
        classifier = EgressClassifier(limits)
        invoker = FakeInvoker(fail=True)
        for host in ("a.example", "b.example"):
            result = await classifier.classify_target(
                host=host,
                port=443,
                tool_name="fetch_url",
                capability="tool.egress",
                invoker=invoker,
            )
            assert result.verdict == "ask"
        assert classifier.degraded
        calls_before = len(invoker.calls)
        result = await classifier.classify_target(
            host="c.example",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=invoker,
        )
        assert result.verdict == "ask"
        assert result.degraded
        assert len(invoker.calls) == calls_before

    @pytest.mark.asyncio
    async def test_budget_exhaustion_fails_safe_to_ask(self):
        classifier = EgressClassifier(
            EgressClassifierLimits(stages="fast", max_calls_per_run=1)
        )
        invoker = FakeInvoker(stage1="allow")
        first = await classifier.classify_target(
            host="one.example",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=invoker,
        )
        second = await classifier.classify_target(
            host="two.example",
            port=443,
            tool_name="fetch_url",
            capability="tool.egress",
            invoker=invoker,
        )
        assert first.verdict == "allow"
        assert second.verdict == "ask"
        assert second.detail == "classifier_budget_exhausted"
        assert len(invoker.calls) == 1


class TestEntrySerialization:
    def test_payload_round_trip(self):
        entry = EgressClassificationEntry(
            fingerprint=classification_fingerprint("example.com"),
            registered_domain="example.com",
            host="docs.example.com",
            port=443,
            verdict="allow",
            source="stage2",
            category="official_docs",
            risk_tags=("documentation",),
            reason="official documentation",
            model="openai:gpt-4.1-mini",
            classified_at=1.0,
        )
        revived = EgressClassificationEntry.from_payload(entry.to_payload())
        assert revived == entry

    def test_invalid_verdict_rejected(self):
        payload = {
            "fingerprint": "egress:x",
            "verdict": "maybe",
            "source": "stage1",
        }
        with pytest.raises(ValueError):
            EgressClassificationEntry.from_payload(payload)

    def test_invalid_source_rejected(self):
        payload = {
            "fingerprint": "egress:x",
            "verdict": "allow",
            "source": "vibes",
        }
        with pytest.raises(ValueError):
            EgressClassificationEntry.from_payload(payload)
