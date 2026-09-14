"""Cross-cutting regressions for evidence, budget, and approval behavior."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from open_deep_research.agents import deep_researcher
from open_deep_research.configuration import Configuration
from open_deep_research.quality.gate import _bound_compressed_research
from open_deep_research.sandbox.approvals import SecurityApproval, SecurityApprovalStore
from open_deep_research.sandbox.gateway import GatewayRunContext, GatewayRuntime
from open_deep_research.tools.web_research import pipeline as web_pipeline


def _accepted_evidence(
    *,
    evidence_id: str = "ev_grounded",
    claim: str = "Accepted grounded fact.",
    source_title: str = "Official source",
    supporting_excerpt: str = "Accepted excerpt.",
) -> dict[str, str]:
    return {
        "evidence_id": evidence_id,
        "claim": claim,
        "source_title": source_title,
        "source_url": "https://example.test/official",
        "supporting_excerpt": supporting_excerpt,
        "security_status": "accepted",
    }


def test_numeric_brackets_copied_from_evidence_are_not_citation_aliases() -> None:
    state = {
        "evidence_registry": [
            _accepted_evidence(
                source_title="Global Top 10 Battery Recyclers [2026]",
                supporting_excerpt="The source paper cross-references method [34].",
            )
        ]
    }

    invalid = deep_researcher._compression_invalid_citations(  # noqa: SLF001
        (
            "Global Top 10 Battery Recyclers [2026]. "
            "The source paper cross-references method [34]. "
            "Grounded finding [ev_grounded]. Bad numeric citation [1]. "
            "Invented alias [made_up]. "
            "A Markdown [source link](https://example.test/official)."
        ),
        state,
    )

    assert invalid == ("1", "made_up")


def test_compression_input_excludes_unverified_tool_candidates() -> None:
    state = {
        "research_topic": "Grounded-only synthesis",
        "researcher_messages": [
            ToolMessage(
                name="web_research",
                tool_call_id="call-1",
                content=(
                    '{"candidates":[{"snippet":"UNVERIFIED_CANDIDATE_FACT"}],'
                    '"evidence":[]}'
                ),
            )
        ],
        "document_registry": [
            {
                "title": "Discovery document without accepted evidence",
                "final_url": "https://candidate.test/unverified",
            }
        ],
        "evidence_registry": [
            _accepted_evidence(claim="ACCEPTED_EVIDENCE_FACT")
        ],
    }

    payload = deep_researcher._compression_evidence_text(state, 20_000)  # noqa: SLF001

    assert "ACCEPTED_EVIDENCE_FACT" in payload
    assert "UNVERIFIED_CANDIDATE_FACT" not in payload
    assert "Discovery document without accepted evidence" not in payload


def test_compression_input_keeps_governed_mcp_and_legacy_results() -> None:
    state = {
        "research_topic": "Mixed tool synthesis",
        "researcher_messages": [
            ToolMessage(
                name="mcp_policy_lookup",
                tool_call_id="mcp-1",
                content=(
                    "MCP_ONLY_FACT https://mcp.example.test/policy"
                ),
            ),
            ToolMessage(
                name="tavily_search",
                tool_call_id="legacy-1",
                content=(
                    "LEGACY_ONLY_FACT https://legacy.example.test/source"
                ),
            ),
            ToolMessage(
                name="legacy_failure",
                tool_call_id="error-1",
                content=(
                    '{"error_type":"timeout","tool_name":"legacy_failure",'
                    '"message":"ERROR_ONLY_TEXT"}'
                ),
            ),
        ],
        "evidence_registry": [],
    }

    payload = deep_researcher._compression_evidence_text(state, 20_000)  # noqa: SLF001
    supplemental = deep_researcher._missing_supplemental_tool_evidence_records(  # noqa: SLF001
        state
    )

    assert "MCP_ONLY_FACT" in payload
    assert "LEGACY_ONLY_FACT" in payload
    assert "ERROR_ONLY_TEXT" not in payload
    assert len(supplemental) == 2
    assert all(record["evidence_id"].startswith("tool_") for record in supplemental)


@pytest.mark.asyncio
async def test_auto_followup_reserve_scales_for_ten_concurrent_units() -> None:
    run_id = "run-7337-followup-reserve"
    configurable = Configuration(
        max_fetches_per_run=40,
        max_fetches_per_researcher=12,
        max_concurrent_research_units=10,
    )
    reserve = web_pipeline._followup_fetch_reserve(configurable)  # noqa: SLF001
    assert reserve == 16

    web_pipeline.clear_run_web_budget(run_id)
    try:
        # Replay the exhausted first-wave cap with five known initial tasks.
        web_pipeline._WEB_RUN_FETCH_ATTEMPTS[run_id] = (  # noqa: SLF001
            configurable.max_fetches_per_run - reserve
        )
        for index in range(5):
            web_pipeline._WEB_TASK_FETCH_ATTEMPTS[(  # noqa: SLF001
                run_id,
                f"initial-{index}",
            )] = 1

        grants = []
        for index in range(4):
            reservation = await web_pipeline._reserve_fetch_budget(  # noqa: SLF001
                {
                    "configurable": configurable.model_dump(mode="json"),
                    "metadata": {
                        "run_id": run_id,
                        "task_id": f"followup-{index}",
                        "research_wave_id": f"wave-{index + 1}",
                    },
                },
                5,
            )
            grants.append(reservation.reserved)

        assert grants == [4, 4, 4, 4]
    finally:
        web_pipeline.clear_run_web_budget(run_id)


def test_handoff_projection_prefers_findings_over_query_trace() -> None:
    query_trace = "Q" * 8_000
    body_sentinel = "US_BODY_FACT_TABLE_SENTINEL"
    full_text = "\n\n".join(
        [
            "**List of Queries and Tool Calls Made**",
            query_trace,
            "**Fully Comprehensive Findings**",
            body_sentinel + "\n" + ("Grounded body fact. " * 500),
            "### Coverage Checklist",
            "\n".join(
                f"- COV-{index:02d}: supported [ev_grounded]"
                for index in range(200)
            ),
            "### Sources",
            "\n".join(
                f"- Source {index}: https://example.test/{index}"
                for index in range(100)
            ),
        ]
    )

    bounded = _bound_compressed_research(full_text, 3_500)

    assert len(bounded) <= 3_500
    assert body_sentinel in bounded
    assert "Coverage Checklist" in bounded


class _ApprovalInternal:
    def __init__(self) -> None:
        self.consume_requests: list[SimpleNamespace] = []

    def signed(self, _request_type, **payload):
        return SimpleNamespace(**payload)

    async def post(self, path, request):
        assert path == "/internal/sandbox/approvals/consume"
        self.consume_requests.append(request)
        return {"status": "consumed"}


@pytest.mark.asyncio
async def test_allow_once_retry_consumes_the_approved_operation() -> None:
    internal = _ApprovalInternal()
    runtime = GatewayRuntime(
        Configuration(
            sandbox_root_signing_key=(
                "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="
            )
        )
    )
    runtime.internal = internal
    context = GatewayRunContext(
        config={"configurable": {}, "metadata": {}},
        fence_token=1,
        expires_at=time.time() + 60,
    )
    target = {"domain": "docs.example.test", "port": 443}
    approval = SecurityApproval(
        approval_id="approval-1",
        run_id="run-1",
        task_id="task-1",
        fence_token=1,
        kind="network",
        capability="tool.egress",
        target=target,
        target_fingerprint=SecurityApprovalStore.fingerprint(
            "network", "tool.egress", target
        ),
        status="resolved",
        decision="allow_once",
        expires_at=time.time() + 60,
        operation_id="original-approved-operation",
    )

    await runtime._consume_network_approval(  # noqa: SLF001
        SimpleNamespace(
            run_id="run-1",
            logical_operation_id="retry-operation",
        ),
        context,
        approval,
    )

    assert len(internal.consume_requests) == 1
    assert (
        internal.consume_requests[0].operation_id
        == "original-approved-operation"
    )
