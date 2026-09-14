"""Business coverage and health boundary cases."""

from datetime import date

from open_deep_research.knowledge.health import calculate, metadata


def doc(**changes):
    result = dict(
        id="doc",
        filename="财报",
        published_id="generation",
        pending_id=None,
        published_at="2025-06-01",
        metadata_snapshot={
            "confirmed": {"company": "竞品甲", "period": "2025年", "doc_type": "财报"}
        },
    )
    result.update(changes)
    return result


def target(**changes):
    result = dict(
        id="target",
        company="竞品甲",
        period="2025年",
        topic="财报",
        min_documents=1,
        max_age_days=0,
    )
    result.update(changes)
    return result


def test_historical_period_is_not_automatically_expired():
    items, coverage, _ = calculate(
        [doc()], [], [], [], [], [target()], date(2026, 9, 9)
    )
    assert not items
    assert coverage[0]["available_documents"] == 1


def test_expiry_is_exclusive_and_expired_documents_do_not_fill_gaps():
    d = doc()
    d["metadata_snapshot"]["confirmed"]["validity"] = {
        "start": "2025-01-01",
        "end": "2026-09-09",
    }
    items, coverage, _ = calculate([d], [], [], [], [], [target()], date(2026, 9, 9))
    assert {i["kind"] for i in items} == {"expired", "missing_topic"}
    assert coverage[0]["missing_documents"] == 1


def test_future_validity_and_draft_do_not_satisfy_coverage():
    d = doc()
    d["metadata_snapshot"]["confirmed"]["validity"] = {"start": "2027-01-01"}
    _, coverage, _ = calculate(
        [d, doc(id="draft", published_id=None, pending_id="draft")],
        [],
        [],
        [],
        [],
        [target()],
        date(2026, 9, 9),
    )
    assert coverage[0]["available_documents"] == 0


def test_absent_competitor_has_missing_topic_and_refresh_policy_is_explicit():
    items, coverage, groups = calculate(
        [doc()],
        [],
        [],
        [],
        [],
        [target(max_age_days=30), target(id="other", company="竞品乙")],
        date(2026, 9, 9),
    )
    assert ("竞品乙", "2025年") in groups
    assert sum(i["kind"] == "missing_topic" for i in items) == 2
    assert coverage[1]["missing_documents"] == 1


def test_pending_fact_sync_failure_and_no_result_are_separate_signals():
    facts = [
        dict(
            id="fact",
            entity_name="竞品甲",
            data_period="2025年",
            period_label="",
            metric="价格",
            status="published",
            verification="disputed",
        )
    ]
    sync = [
        dict(
            id="sync",
            document_id="doc",
            consecutive_failures=3,
            paused=True,
            last_success_at=None,
        )
    ]
    queries = [
        dict(
            id="query",
            scope={"resolved_document_ids": ["doc"]},
            query_text="价格？",
            created_at="2026-09-09",
        )
    ]
    items, _, _ = calculate([doc()], facts, [], sync, queries, [], date(2026, 9, 9))
    assert {i["kind"] for i in items} == {"pending_review", "sync_failed", "no_results"}
    assert all(i["company"] == "竞品甲" for i in items)


def test_multiple_competitor_query_is_not_inferred_as_one_competitor():
    queries = [
        dict(
            id="query",
            scope={"kb_ids": ["one", "two"], "resolved_document_ids": ["doc"]},
            query_text="对比？",
            created_at="2026-09-09",
        )
    ]
    items, _, _ = calculate([doc()], [], [], [], queries, [], date(2026, 9, 9))
    assert items[0]["company"] == "未标注竞品"


def test_suggested_metadata_is_not_confirmed_coverage():
    assert metadata({"suggested": {"company": {"value": "竞品甲"}}}) == {}
    assert (
        metadata({"confirmed": {"company": {"name": "竞品甲"}}})["company"] == "竞品甲"
    )
