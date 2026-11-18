"""Bounded uncertainty stays visible without becoming an invented rewrite task."""

import pytest

from open_deep_research.report import reviewer
from open_deep_research.report.models import ReportDraft


@pytest.mark.parametrize("bounded,advisory,missing", [
    (True, True, False), (False, True, False), (True, False, False), (True, True, True),
])
def test_only_individually_disclosed_content_gaps_can_be_nonblocking(bounded, advisory, missing):
    url = "https://docs.example/product"
    fact = "The product supports document retrieval."
    draft = ReportDraft(markdown=f"# Comparison\n\n{fact} [Source]({url})\n\nThe checked material does not establish a price; the price remains unverified.")
    state = {
        "completion_outcome": {"action": "complete_partial" if bounded else "complete"},
        "coverage_contract": {"schema_version": 1, "requirements": [
            {"requirement_id": "feature", "kind": "factual", "text": "Describe retrieval support."},
            {"requirement_id": "price", "kind": "factual", "text": "Verify the price, or disclose the bounded uncertainty."},
            {"requirement_id": "execution", "kind": "process", "text": "Read the research skills before any page fetch."},
        ]},
        "evidence_registry": [{"evidence_id": "feature-evidence", "source_url": url,
                               "claim": fact, "supporting_excerpt": fact, "security_status": "accepted"}],
    }
    raw = {
        "decision": "pass", "dimensions": {name: .95 for name in reviewer._DIMENSIONS},
        "coverage": [
            {"requirement_id": "feature", "status": "covered", "explanation": "Supported",
             "answer_excerpt": fact, "supporting_evidence": [{"evidence_id": "feature-evidence", "quote": fact}], "missing_aspects": []},
            {"requirement_id": "price", "status": "missing" if missing else "partial",
             "explanation": "The bounded uncertainty is explicitly disclosed.", "missing_aspects": ["verified price"]},
            {"requirement_id": "execution", "status": "partial",
             "explanation": "Execution receipts were not submitted to the report reviewer."},
        ],
        "citation_audit": [{"claim": fact, "citation_target": url, "supported": True, "evidence_ids": ["feature-evidence"]}],
        "issues": ([{"category": "coverage", "severity": "info", "requirement_id": "price",
                     "description": "The report accurately discloses the bounded price gap.",
                     "revision_instruction": "No change needed.", "blocking": False, "requires_change": False}]
                   if advisory else []),
    }
    config = {"configurable": {"report_review_fail_open": False},
              "metadata": {"run_config_schema_version": 23, "quality_policy_version": "quality-gate-v6"}}
    result = reviewer._normalize_candidate(raw, draft=draft, state=state, config=config,
        payload=reviewer.build_reviewer_payload(draft, state, config), model_name="fixture", attempt=1)
    assert result.decision == ("pass" if bounded and advisory and not missing else "revise")
    assert result.coverage[1].status == ("missing" if missing else "partial")
    assert result.coverage[2].status == "partial"
    assert result.dimensions.coverage == (.5 if missing else .75)


def test_irrelevant_old_boundary_cases_do_not_expand_a_competitor_report():
    state = {"coverage_contract": {"requirements": [{"requirement_id": "compare", "kind": "factual",
        "text": "比较两款知识库产品的功能、部署及入门定价。"}]},
        "report_review_history": [{"provenance": {"boundary_applicable_cases": ["helper_first", "helper_after"]}}]}
    config = {"metadata": {"run_config_schema_version": 23, "quality_policy_version": "quality-gate-v6"}}
    payload = reviewer.build_reviewer_payload(ReportDraft(markdown="# Product comparison"), state, config)
    assert payload["required_boundary_cases"] == []
