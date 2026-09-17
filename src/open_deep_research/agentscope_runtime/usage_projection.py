"""Content-free browser accounting from the authoritative native SQL receipts."""

from sqlalchemy import select


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
                        store.ops.c.kind.in_(["gateway:model", "model_attempt"]),
                    )
                )
            )
            .mappings()
            .all()
        )
    totals = response["totals"]
    calls = totals["calls"]
    costs = []
    cost_reported = []
    for row in rows:
        calls["attempts"] += 1
        result = row["result"] or {}
        correction = result.get("usage_correction")
        reported = bool(correction) or result.get("usage_status") == "reported"
        outcome = result.get("outcome") or {}
        usage = (
            (correction or {}).get("usage")
            or outcome.get("usage")
            or row["actual"]
            or {}
        )
        if row["state"] != "committed":
            calls["unknown_failed_attempts"] += 1
            calls["missing"] += 1
            continue
        if outcome.get("usage") and all(
            outcome["usage"].get(k) is not None
            for k in ("input_tokens", "output_tokens")
        ):
            reported = True
        vector = totals["reported" if reported else "estimated"]
        for name in vector:
            if name != "total_tokens":
                vector[name] += usage.get(name) or 0
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
        tool_call_count=budget["used"].get("tool_calls", 0),
    )
    return response
