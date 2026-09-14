"""LiteLLM spend-log and key-list analytics client tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from open_deep_research.models.credentials import RunKeySettings
from open_deep_research.models.spend import (
    GatewayKeySpend,
    LiteLLMSpendClient,
    aggregate_spend_logs_by_tag_prefix,
    parse_key_spend_entries,
    run_spend_index,
    usd_to_micro_usd,
)


def settings() -> RunKeySettings:
    return RunKeySettings(
        base_url="http://litellm-proxy:4000/v1",
        master_key="master",
        team_id="insightforge-runs",
        encryption_key=b"k" * 32,
        default_budget_micro_usd=2_000_000,
        maximum_budget_micro_usd=5_000_000,
    )


def test_usd_to_micro_usd_is_defensive() -> None:
    assert usd_to_micro_usd("1.234567") == 1_234_567
    assert usd_to_micro_usd(0.5) == 500_000
    assert usd_to_micro_usd(None) == 0
    assert usd_to_micro_usd("NaN") == 0
    assert usd_to_micro_usd("not-a-number") == 0


def test_key_alias_run_attribution_and_reissue_summation() -> None:
    entries = parse_key_spend_entries(
        [
            {"key_alias": "run-abc123-a1b2c3d4", "spend": "1.000000"},
            {"key_alias": "run-abc123-e5f6a7b8", "spend": 0.5},
            {"key_alias": "run-shortdead", "spend": "9"},  # malformed tail
            {"key_alias": "insightforge-service", "spend": "3"},  # non-run
            {"spend": "4"},  # aliasless
        ]
    )
    index = run_spend_index(entries)
    assert index == {"abc123": 1_500_000}

    parsed = {entry.key_alias: entry for entry in entries}
    assert parsed["run-abc123-a1b2c3d4"].run_id == "abc123"
    assert parsed["insightforge-service"].run_id is None
    assert parsed["run-shortdead"].run_id is None


def test_stage_tag_aggregation_skips_untagged_logs() -> None:
    logs = [
        {
            "request_tags": ["run:run-1", "stage:researching", "role:researcher"],
            "total_tokens": 100,
            "spend": "0.001",
        },
        {
            "request_tags": ["run:run-1", "stage:researching"],
            "total_tokens": 50,
            "spend": 0.0005,
        },
        {
            "request_tags": ["run:run-1", "stage:writing"],
            "total_tokens": 30,
            "spend": "0.002",
        },
        {"request_tags": [], "total_tokens": 999, "spend": "9"},  # skipped
        {"total_tokens": 5},  # no tags at all, skipped
    ]
    by_stage = aggregate_spend_logs_by_tag_prefix(logs, "stage:")
    assert [(bucket.key, bucket.calls, bucket.total_tokens) for bucket in by_stage] == [
        ("researching", 2, 150),
        ("writing", 1, 30),
    ]
    assert by_stage[0].spend_micro_usd == 1_500
    assert by_stage[1].spend_micro_usd == 2_000

    by_role = aggregate_spend_logs_by_tag_prefix(logs, "role:")
    assert [(bucket.key, bucket.calls) for bucket in by_role] == [("researcher", 1)]


def test_list_keys_with_spend_paginates_and_stops() -> None:
    requested: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requested.append(params)
        page = int(params["page"])
        if page == 1:
            keys = [
                {"key_alias": f"run-run{index:02d}-a1b2c3d4", "spend": "1.0"}
                for index in range(3)
            ]
            keys.append({"key_alias": "insightforge-service", "spend": "2"})
            return httpx.Response(200, json={"keys": keys})
        return httpx.Response(200, json={"keys": []})

    client = LiteLLMSpendClient(
        settings(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://litellm-proxy:4000",
            headers={"Authorization": "Bearer master"},
        ),
    )

    async def run():
        try:
            return await client.list_keys_with_spend(page_size=4)
        finally:
            await client.aclose()

    entries = asyncio.run(run())

    assert requested[0]["page"] == "1"
    assert requested[0]["return_full_object"] == "true"
    assert len(requested) == 2  # second page returned short/empty and stopped
    assert len(entries) == 4
    assert sum(entry.spend_micro_usd for entry in entries) == 5_000_000


def test_spend_logs_accepts_bare_array_and_data_shapes_and_filters_by_run() -> None:
    requested: list[dict[str, str]] = []
    run_rows = [
        {
            "request_tags": ["run:run-1", "stage:planning"],
            "total_tokens": 10,
            "spend": "0.1",
        },
        {
            "request_tags": ["run:run-1", "stage:writing"],
            "total_tokens": 7,
            "spend": "0.02",
        },
    ]
    responses: list[Any] = [
        # 1.98 returns a bare JSON array (first page, includes another run).
        run_rows
        + [
            {
                "request_tags": ["run:run-2", "stage:researching"],
                "total_tokens": 99,
                "spend": "9",
            }
        ],
        # Older shapes wrap rows in {"data": [...]} (second page, short page).
        {"data": [run_rows[0]], "total": 3},
        # run_spend_logs fetch: bare array again.
        run_rows,
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(dict(request.url.params))
        return httpx.Response(200, json=responses.pop(0))

    client = LiteLLMSpendClient(
        settings(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://litellm-proxy:4000",
            headers={"Authorization": "Bearer master"},
        ),
    )

    async def run():
        try:
            logs = await client.spend_logs(api_key="sk-run-key", size=2)
            assert len(logs) == 4
            buckets = aggregate_spend_logs_by_tag_prefix(logs, "stage:")
            assert [(b.key, b.calls) for b in buckets] == [
                ("planning", 2),
                ("researching", 1),
                ("writing", 1),
            ]

            mine = await client.run_spend_logs("run-1", size=10, max_pages=1)
            assert len(mine) == 2
            assert all("run:run-1" in mine[i]["request_tags"] for i in range(2))
        finally:
            await client.aclose()

    asyncio.run(run())

    assert requested[0]["api_key"] == "sk-run-key"
    # run_spend_logs issues its own unfiltered fetch.
    assert "api_key" not in requested[-1]


def test_unexpected_payload_shapes_break_cleanly() -> None:
    assert parse_key_spend_entries([{"unexpected": True}, "junk"]) == []
    assert run_spend_index([]) == {}
    assert aggregate_spend_logs_by_tag_prefix(
        [{"request_tags": "not-a-list", "spend": "1"}], "stage:"
    ) == []
    assert json.dumps(GatewayKeySpend("a", 1, None, ()).models) == "[]"  # serializable tuple
