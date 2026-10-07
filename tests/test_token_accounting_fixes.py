"""Native SQL accounting and read-only compatibility usage regressions."""

from __future__ import annotations

import sqlite3
import time
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from open_deep_research.agentscope_runtime.gateway_ledger import SQLGatewayLedger
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
from open_deep_research.api.usage import _build_usage_analytics_response
from open_deep_research.configuration import Configuration, frozen_run_config_values
from open_deep_research.observability import SQLiteTraceStore, TokenUsage
from open_deep_research.sandbox.internal_api import (
    BudgetReserveRequest,
    OperationTransitionRequest,
)


@pytest_asyncio.fixture
async def native_ledger(tmp_path):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "usage.db").as_posix())
    await store.create_tables()
    await store.create_run(
        "owner",
        ResearchSnapshot(run_id="usage", config_fingerprint="f"),
        limits={
            "model_calls": 10,
            "input_tokens": 1000,
            "output_tokens": 1000,
            "cost_micro_usd": 10000,
        },
    )
    recovery = await RecoverySession.open(store, "usage", "owner")
    ledger = SQLGatewayLedger(
        recovery,
        {
            "openai:gpt-priced": {
                "input_cost_per_token": 0.000002,
                "output_cost_per_token": 0.000004,
            }
        },
    )
    yield ledger
    await recovery.close()
    await store.aclose()


async def reserve(ledger, operation):
    await ledger.reserve(
        BudgetReserveRequest(
            run_id="usage",
            task_id="t",
            fence_token=ledger.recovery.lease.fence,
            stage="researching",
            logical_operation_id=operation,
            physical_attempt_id=operation,
            request_digest=operation,
            agent_role="researcher",
            model_name="openai:gpt-priced",
            estimated_input_tokens=100,
            estimated_output_tokens=50,
            service_nonce="native-fixture-nonce",
            service_timestamp=time.time(),
            service_signature="fixture",
        )
    )


@pytest.mark.asyncio
async def test_full_provider_model_price_key_reaches_budget_boundary(native_ledger):
    await reserve(native_ledger, "priced")
    session = native_ledger.recovery
    budget = await session.store.budget("usage", "owner")
    assert budget["reserved"]["cost_micro_usd"] == 400
    await native_ledger.transition(
        OperationTransitionRequest(
            run_id="usage",
            fence_token=session.lease.fence,
            logical_operation_id="priced",
            status="completed",
            outcome={
                "requested_model": "openai:gpt-priced",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
            service_nonce="native-fixture-nonce",
            service_timestamp=time.time(),
            service_signature="fixture",
        )
    )
    assert (await session.store.budget("usage", "owner"))["used"][
        "cost_micro_usd"
    ] == 14


@pytest.mark.asyncio
async def test_deterministic_rejection_releases_tokens_but_unknown_call_retains_reservation(
    native_ledger,
):
    await reserve(native_ledger, "rejected")
    session = native_ledger.recovery
    await native_ledger.transition(
        OperationTransitionRequest(
            run_id="usage",
            fence_token=session.lease.fence,
            logical_operation_id="rejected",
            status="failed",
            outcome={"error_code": "permission_denied"},
            service_nonce="native-fixture-nonce",
            service_timestamp=time.time(),
            service_signature="fixture",
        )
    )
    await reserve(native_ledger, "unknown")
    budget = await session.store.budget("usage", "owner")
    assert budget["used"]["model_calls"] == 1
    assert budget["used"]["input_tokens"] == 0
    assert budget["reserved"]["input_tokens"] == 100
    assert budget["reserved"]["cost_micro_usd"] == 400


def test_provider_filter_aggregates_only_matching_usage(tmp_path) -> None:
    store = SQLiteTraceStore(str(tmp_path / "filter.sqlite3"))
    store.start_run("filter-run", "owner-1", {})
    for index, (provider, total) in enumerate(
        (("openai", 5), ("anthropic", 11)),
        start=1,
    ):
        span_id = f"span-{index}"
        store.start_span(
            span_id=span_id,
            run_id="filter-run",
            parent_span_id=None,
            name="test.model",
            kind="llm",
            agent_role="researcher",
            attributes={},
            input_preview=None,
            provider=provider,
            model="model-test",
        )
        store.add_usage(
            "filter-run",
            span_id,
            provider,
            "model-test",
            TokenUsage(input_tokens=total - 1, output_tokens=1, total_tokens=total),
            event_key=f"event-{index}",
            stage="researching",
        )

    report = store.get_usage_accounting("filter-run", provider="openai")
    batch_report = store.get_usage_accounting_many(["filter-run"], provider="openai")[
        "filter-run"
    ]

    assert report["totals"]["reported"]["total_tokens"] == 5
    assert batch_report["totals"] == report["totals"]
    assert batch_report["operations"]["llm_call_count"] == 1
    assert [bucket["key"] for bucket in report["breakdowns"]["by_model"]] == [
        "openai:model-test"
    ]


def test_legacy_llm_span_without_usage_is_marked_unclassified(tmp_path) -> None:
    path = tmp_path / "legacy-missing.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE spans (
                span_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                parent_span_id TEXT, name TEXT NOT NULL, kind TEXT NOT NULL,
                agent_role TEXT, status TEXT NOT NULL, started_at REAL NOT NULL,
                ended_at REAL, duration_ms INTEGER, model TEXT, provider TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                attributes_json TEXT NOT NULL DEFAULT '{}', input_preview TEXT,
                output_preview TEXT, error TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                span_id TEXT NOT NULL, provider TEXT, model TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                raw_usage_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO spans (
                span_id, run_id, name, kind, agent_role, status, started_at,
                ended_at, duration_ms, model, provider
            ) VALUES ('old-span', 'old-run', 'legacy.model', 'llm',
                      'lead', 'success', 1, 2, 1000, 'gpt-old', 'openai')"""
        )

    store = SQLiteTraceStore(str(path))
    accounting = store.get_usage_accounting("old-run")

    assert accounting["totals"]["calls"]["legacy_unclassified"] == 1
    assert accounting["totals"]["reported"]["total_tokens"] == 0
    assert accounting["accounting_status"] == "partial"


def test_zero_retention_means_forever_and_history_filters_usage_rows(
    tmp_path,
    monkeypatch,
) -> None:
    trace_path = tmp_path / "history.sqlite3"
    monkeypatch.setenv("TRACE_STORE_PATH", str(trace_path))
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("TRACE_RETENTION_DAYS", "0")
    store = SQLiteTraceStore(str(trace_path))
    store.start_run("old-run", "owner-1", {"title": "Old run"})
    with sqlite3.connect(trace_path) as conn:
        conn.execute(
            "UPDATE runs SET started_at = ?, status = 'success' WHERE run_id = ?",
            (time.time() - 100 * 86400, "old-run"),
        )
    for index, (provider, total) in enumerate(
        (("openai", 5), ("anthropic", 11)),
        start=1,
    ):
        span_id = f"history-span-{index}"
        store.start_span(
            span_id=span_id,
            run_id="old-run",
            parent_span_id=None,
            name="history.model",
            kind="llm",
            agent_role="researcher",
            attributes={},
            input_preview=None,
            provider=provider,
            model="history-model",
        )
        store.add_usage(
            "old-run",
            span_id,
            provider,
            "history-model",
            TokenUsage(input_tokens=total - 1, output_tokens=1, total_tokens=total),
            event_key=f"history-event-{index}",
            stage="researching",
        )

    retained = _build_usage_analytics_response(
        range_name="retained",
        status="completed",
        provider="openai",
        model=None,
        query=None,
        timezone_name="UTC",
        timezone_info=ZoneInfo("UTC"),
        limit=50,
        offset=0,
        user_id="owner-1",
    )
    recent = _build_usage_analytics_response(
        range_name="7d",
        status=None,
        provider=None,
        model=None,
        query=None,
        timezone_name="UTC",
        timezone_info=ZoneInfo("UTC"),
        limit=50,
        offset=0,
        user_id="owner-1",
    )

    assert retained["summary"]["run_count"] == 1
    assert retained["summary"]["reported"]["total_tokens"] == 5
    assert retained["actual_range_days"] >= 99
    assert recent["summary"]["run_count"] == 0


def test_usage_projection_exposes_outstanding_budget_and_retry_timeline(
    tmp_path,
) -> None:
    store = SQLiteTraceStore(str(tmp_path / "projection.sqlite3"))
    store.start_run("projection-run", "owner-1", {})
    store.start_span(
        span_id="projection-span",
        run_id="projection-run",
        parent_span_id=None,
        name="projection.model",
        kind="llm",
        agent_role="researcher",
        attributes={},
        input_preview=None,
        provider="openai",
        model="gpt-test",
    )
    store.add_usage(
        "projection-run",
        "projection-span",
        "openai",
        "gpt-test",
        TokenUsage(input_tokens=4, output_tokens=2, total_tokens=6),
        event_key="projection-event",
        stage="writing",
    )
    store.record_retry_event(
        run_id="projection-run",
        span_id="projection-span",
        attempt=1,
        error_type="rate_limited",
    )

    report = store.get_usage_accounting(
        "projection-run",
        reserved_budget={
            "input_tokens": 20,
            "output_tokens": 30,
            "model_calls": 1,
            "cost_micro_usd": 40,
        },
    )

    assert report["totals"]["budgets"]["input_tokens"]["reserved"] == 20
    assert report["totals"]["budgets"]["model_calls"]["reserved"] == 1
    assert sum(bucket["retry_count"] for bucket in report["timeline"]) == 1
    assert report["breakdowns"]["by_stage"][0]["key"] == "writing"


def test_no_usage_has_specific_unavailable_reason(tmp_path) -> None:
    store = SQLiteTraceStore(str(tmp_path / "empty.sqlite3"))
    store.start_run("empty-run", "owner-1", {})

    report = store.get_usage_accounting("empty-run")

    assert report["accounting_status"] == "unavailable"
    assert report["unavailable_reason"] == "no_usage_events"


def test_configuration_accepts_unlimited_trace_retention() -> None:
    configuration = Configuration(trace_retention_days=0)
    assert configuration.trace_retention_days == 0


def test_v5_fingerprint_contract_excludes_v6_token_fields() -> None:
    values = {
        "token_usage_accounting_enabled": False,
        "token_usage_estimation_enabled": False,
        "model_costs_per_million": {"openai:gpt-test": {"input": 1}},
    }
    old = frozen_run_config_values(
        {
            "configurable": values,
            "metadata": {"run_config_schema_version": 5},
        }
    )
    current = frozen_run_config_values(
        {
            "configurable": values,
            "metadata": {"run_config_schema_version": 6},
        }
    )

    assert not set(values).intersection(old)
    assert set(values).issubset(current)
