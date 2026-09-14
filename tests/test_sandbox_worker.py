from open_deep_research.sandbox.worker import _deduplicate_iterations


def test_worker_deduplicates_replayed_web_iterations() -> None:
    iteration = {
        "request": {"queries": ["query"]},
        "gap_analysis": {"decision": "budget_exhausted"},
        "approval_batch": {"run_id": "run-1"},
    }

    assert _deduplicate_iterations(
        [iteration, dict(iteration), {"request": {"queries": ["other"]}}]
    ) == [iteration, {"request": {"queries": ["other"]}}]
