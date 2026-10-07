"""Content-free browser accounting from the authoritative native SQL receipts."""

from sqlalchemy import select

from open_deep_research.agentscope_runtime.recovery_events import _STAGES


def operation_context(row, starts, ends):
    """Use recorded dimensions; old receipts remain explicitly unattributed."""
    meta = starts.get(row["key"], {})
    end = ends.get(row["key"])
    start = meta.get("timestamp")
    return {
        **meta,
        "finished_at": end,
        "duration_ms": max(0, (end - start) * 1000)
        if end is not None and start is not None
        else None,
    }


def tool_success(row):
    if row["state"] != "committed":
        return None
    result = row["result"] or {}
    outcome = result.get("outcome") if row["kind"] == "gateway:tool" else result
    if outcome is None:
        return None
    return (
        not bool(outcome.get("error"))
        and outcome.get("status", "completed") == "completed"
    )


_PURPOSE_LABELS = {
    "supervisor": "调度与规划", "researcher": "研究推理", "context_summary": "上下文摘要",
    "handoff_compression": "研究交接压缩", "web_evidence": "证据提取", "web_rerank": "来源重排",
    "researcher.evaluate_tool_results": "工具批次评估", "supervisor.evaluate_handoff": "交接评估",
    "lead.report_review": "报告复核", "final_report": "报告生成", "approval_outline": "报告大纲",
    "egress_classification": "出网分类", "message_summary": "用户消息摘要", "unknown": "未归属",
}


def breakdown(records, key):
    groups = {}
    for record in records:
        value = record.get(key) or "unknown"
        groups.setdefault(value, []).append(record)
    output = []
    for value, items in sorted(groups.items()):
        vectors = {
            source: {
                name: sum(item[source][name] for item in items)
                for name in items[0][source]
            }
            for source in ("reported", "estimated")
        }
        costs = [item["cost"] for item in items if item["cost"] is not None]
        durations = [
            item["duration_ms"] for item in items if item["duration_ms"] is not None
        ]
        output.append(
            {
                "key": value,
                "label": _PURPOSE_LABELS.get(value, value) if key == "purpose" else value,
                **vectors,
                "call_count": len(items),
                "estimated_cost_micro_usd": sum(costs) if costs else None,
                "cost_source": "provider_reported"
                if costs and all(item["cost_reported"] for item in items)
                else "configured_estimate"
                if costs
                else "unavailable",
                "average_latency_ms": sum(durations) / len(durations)
                if durations
                else None,
                "completeness": "complete"
                if all(item["reported_complete"] for item in items)
                else "partial",
            }
        )
    return output


def timeline(records):
    timed = [item for item in records if item["finished_at"] is not None]
    if not timed:
        return []
    start = min(item["finished_at"] for item in timed)
    width = max(1, (max(item["finished_at"] for item in timed) - start) / 119)
    buckets = {}
    for item in timed:
        index = min(119, int((item["finished_at"] - start) / width))
        buckets.setdefault(index, []).append(item)
    output, reported, estimated = [], 0, 0
    for index, items in sorted(buckets.items()):
        r = sum(item["reported"]["total_tokens"] for item in items)
        e = sum(item["estimated"]["total_tokens"] for item in items)
        reported += r
        estimated += e
        output.append(
            {
                "timestamp": start + index * width,
                "reported_tokens": r,
                "estimated_tokens": e,
                "reported_cumulative": reported,
                "estimated_cumulative": estimated,
                "call_count": len(items),
                "retry_count": sum(item["retry_count"] for item in items),
            }
        )
    return output


async def project_usage(store, run_id, owner, response):
    """Fill the shared HTTP shape without classifying reservations as reported."""
    state, revision = await store.load(run_id, owner)
    budget = await store.budget(run_id, owner)
    async with store.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(store.ops).where(
                        store.ops.c.run_id == run_id,
                        store.ops.c.kind.in_(
                            ["gateway:model", "model_attempt", "tool", "gateway:tool"]
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )
        events = (
            (
                await conn.execute(
                    select(store.outbox.c.payload).where(
                        store.outbox.c.run_id == run_id,
                        store.outbox.c.payload["type"].as_string().in_([
                            "research.operation_started", "research.operation_committed", "research.state", "research.cancelled"
                        ]),
                    )
                )
            )
            .scalars()
            .all()
        )
    starts = {
        e["operation_key"]: e
        for e in events
        if e["type"] == "research.operation_started"
    }
    ends = {
        e["operation_key"]: e["timestamp"]
        for e in events
        if e["type"] == "research.operation_committed"
    }
    tool_rows = [
        r
        for r in rows
        if r["kind"] in {"tool", "gateway:tool"}
        and (r["reservation"] or {}).get("tool_calls")
    ]
    rows = [r for r in rows if r["kind"] in {"gateway:model", "model_attempt"}]
    totals = response["totals"]
    calls = totals["calls"]
    costs = []
    cost_reported = []
    records = []
    task_operations = {}
    for row in rows:
        calls["attempts"] += 1
        result = row["result"] or {}
        correction = result.get("usage_correction")
        reported = bool(correction) or result.get("usage_status") == "reported"
        outcome = result.get("outcome") or {}
        # A later bill corrects billed quantities, not the provider's other
        # observations. Preserve cache facts absent from that correction.
        usage = dict(row["actual"] or {})
        usage.update({k: v for k, v in (outcome.get("usage") or {}).items() if v is not None})
        usage.update((correction or {}).get("usage") or {})
        meta = operation_context(row, starts, ends)
        task_id = meta.get("task_id") or "unknown"
        task_operations.setdefault(
            task_id,
            {
                "model_call_count": 0,
                "tool_call_count": 0,
                "tool_success_count": 0,
                "tool_failed_count": 0,
            },
        )["model_call_count"] += 1
        if row["state"] != "committed":
            calls["unknown_failed_attempts"] += int(row["state"] == "quarantined")
            calls["missing"] += 1
            continue
        if outcome.get("usage") and all(
            outcome["usage"].get(k) is not None
            for k in ("input_tokens", "output_tokens")
        ):
            reported = True
        vector = totals["reported" if reported else "estimated"]
        normalized = {name: usage.get(name) or result.get(name) or 0 for name in vector}
        normalized["total_tokens"] = (
            normalized["input_tokens"] + normalized["output_tokens"]
        )
        for name in vector:
            if name != "total_tokens":
                vector[name] += normalized[name]
        calls["provider_reported" if reported else "estimated"] += 1
        if result.get("status") == "completed" or result.get("response"):
            calls["successful_responses"] += 1
        if (row["actual"] or {}).get("cost_micro_usd") is not None:
            costs.append(row["actual"]["cost_micro_usd"])
            cost_reported.append(
                bool(correction)
                or outcome.get("response_cost_usd") is not None
                or result.get("cost_status") == "reported"
            )
        records.append(
            {
                **meta,
                "task_id": task_id,
                "stage": _STAGES.get(
                    meta.get("stage"),
                    "researching"
                    if meta.get("stage") == "team-worker"
                    else meta.get("stage"),
                ),
                "agent_role": meta.get("agent_role"),
                "model": meta.get("model") or outcome.get("requested_model"),
                "reported": normalized if reported else dict.fromkeys(vector, 0),
                "estimated": dict.fromkeys(vector, 0) if reported else normalized,
                "reported_complete": reported,
                "cost": (row["actual"] or {}).get("cost_micro_usd"),
                "cost_reported": bool(correction)
                or outcome.get("response_cost_usd") is not None
                or result.get("cost_status") == "reported",
                "retry_count": outcome.get("logical_retry_count", 0),
                "rate_limited": outcome.get("error_code") == "rate_limit"
                or result.get("status_code") == 429,
                "cache_write_reported": usage.get("cache_creation_input_tokens") is not None or result.get("cache_creation_input_tokens") is not None,
                "cache_reported": usage.get("cached_input_tokens") is not None or result.get("cached_input_tokens") is not None,
            }
        )
    for vector in (totals["reported"], totals["estimated"]):
        vector["total_tokens"] = vector["input_tokens"] + vector["output_tokens"]
    calls["coverage_ratio"] = (
        calls["provider_reported"] / calls["attempts"] if calls["attempts"] else 0
    )
    totals["budgets"] = {
        key: {
            "settled": budget["used"].get(key, 0),
            "estimated": 0,
            "reserved": budget["reserved"].get(key, 0),
            "limit": budget["limits"].get(key),
        }
        for key in set(budget["used"]) | set(budget["reserved"]) | set(budget["limits"])
    }
    totals["cost"].update(
        estimated_cost_micro_usd=sum(costs) if costs else None,
        cost_source=(
            "provider_reported" if all(cost_reported) else "configured_estimate"
        )
        if costs
        else "unavailable",
    )
    response.update(
        status=state.status,
        revision=revision,
        accounting_status="partial"
        if calls["missing"] or calls["estimated"]
        else "complete",
    )
    response.pop("unavailable_reason", None)
    response["operations"].update(
        llm_call_count=calls["attempts"],
        tool_call_count=len(tool_rows),
    )
    successes = failures = 0
    for row in tool_rows:
        task = starts.get(row["key"], {}).get("task_id") or "unknown"
        counts = task_operations.setdefault(
            task,
            {
                "model_call_count": 0,
                "tool_call_count": 0,
                "tool_success_count": 0,
                "tool_failed_count": 0,
            },
        )
        counts["tool_call_count"] += 1
        ok = tool_success(row)
        if ok is not None:
            successes += int(ok)
            failures += int(not ok)
            counts["tool_success_count" if ok else "tool_failed_count"] += 1
    response["task_operations"] = task_operations
    response["breakdowns"] = {
        name: breakdown(records, key)
        for name, key in (
            ("by_stage", "stage"),
            ("by_agent_role", "agent_role"),
            ("by_model", "model"),
            ("by_task", "task_id"),
            ("by_purpose", "purpose"),
        )
    }
    response["timeline"] = timeline(records)
    duration = sum(item["duration_ms"] or 0 for item in records) / 1000
    response["operations"].update(
        tool_success_rate=successes / (successes + failures)
        if successes + failures
        else None,
        tool_success_count=successes,
        tool_failed_count=failures,
        tool_pending_count=sum(row["state"] == "started" for row in tool_rows),
        retry_count=sum(item["retry_count"] for item in records),
        rate_limited_count=sum(item["rate_limited"] for item in records),
        rate_429=sum(item["rate_limited"] for item in records) / len(records)
        if records
        else 0,
        output_tokens_per_second=totals["reported"]["output_tokens"] / duration
        if duration
        else 0,
    )
    terminal_times = [event["timestamp"] for event in events
                      if event["type"] == "research.cancelled" or
                      (event["type"] == "research.state" and event.get("status") in {"completed", "failed", "cancelled"})]
    started_at = state.application.get("created_at")
    if started_at is not None:
        import time

        finished_at = max(terminal_times) if terminal_times else None
        if finished_at is not None or state.status not in {"completed", "failed", "cancelled"}:
            response["duration_ms"] = max(0, int(((finished_at or time.time()) - started_at) * 1000))
    response["updated_at"] = max(ends.values(), default=None)
    cache_records = [item for item in records if item["cache_reported"]]
    cached = sum(item["reported"]["cached_input_tokens"] for item in cache_records)
    known_input = sum(item["reported"]["input_tokens"] for item in cache_records)
    response["cache_reporting"] = {
        "status": "reported" if records and len(cache_records) == len(records) else "partial" if cache_records else "not_reported",
        "reported_calls": len(cache_records), "not_reported_calls": len(records) - len(cache_records),
        "cached_input_tokens": cached,
        "non_cached_input_tokens": known_input - cached if len(cache_records) == len(records) and records else None,
        "cache_creation_input_tokens": totals["reported"]["cache_creation_input_tokens"] if any(item["cache_write_reported"] for item in records) else None,
    }
    response["operations"]["cache_hit_rate"] = sum(item["reported"]["cached_input_tokens"] > 0 for item in cache_records) / len(cache_records) if cache_records else None
    response["operations"]["cache_input_ratio"] = cached / known_input if known_input else None
    calls["logical_calls"] = len({item["logical_call_id"] for item in records}) if records and all(item.get("logical_call_id") for item in records) else None
    calls["gateway_requests"] = sum(row["kind"] == "gateway:model" for row in rows)
    calls["upstream_attempts"] = None
    research = await store.research_progress_view(run_id, owner)
    from open_deep_research.agentscope_runtime.efficiency import progress_summary

    response["research_progress"] = progress_summary(research, state.coverage_contract)
    response["efficiency"] = research.get("counters", {})
    response["tool_durations"] = [{"tool_name": starts.get(row["key"], {}).get("tool_name", "unknown"),
        "task_id": starts.get(row["key"], {}).get("task_id", "unknown"),
        "duration_ms": operation_context(row, starts, ends)["duration_ms"]} for row in tool_rows]
    return response
