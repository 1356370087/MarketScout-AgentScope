"""Local, redacted evaluation views of authoritative native SQL receipts."""

import json

from sqlalchemy import select

from .snapshot import _is_sensitive_key, _redact_secret_text, build_evaluation_snapshot


class EvaluationRecorder:
    """Private SQL events at the real governed tool boundary, including supervisor tools."""

    def __init__(self, recovery):
        self.recovery = recovery

    async def emit(self, kind, key, payload):
        from open_deep_research.agentscope_runtime.recovery_store import digest

        session = self.recovery
        event_id = digest(["evaluation", kind, key])
        async with session.store.transaction(session.lease) as (conn, row):
            exists = await conn.scalar(
                select(session.store.outbox.c.event_id).where(
                    session.store.outbox.c.run_id == session.lease.run_id,
                    session.store.outbox.c.event_id == event_id,
                )
            )
            if exists is None:
                await session.store._event(
                    conn, row, kind, project_request(payload), event_id=event_id
                )

    async def request(self, operation_id, task_id, role, tool, call_id, args):
        await self.emit(
            "evaluation.tool_requested",
            operation_id,
            {
                "operation_key": operation_id,
                "task_id": task_id,
                "role": role,
                "name": tool.name,
                "id": call_id,
                "args": args,
                "effect": tool.effect.value,
                "execution_zone": tool.execution_zone.value,
            },
        )

    async def outcome(self, operation_id, task_id, call_id, *, error_type, output=None):
        text = (
            json.dumps(output, ensure_ascii=False, default=str)
            if output is not None
            else ""
        )
        await self.emit(
            "evaluation.tool_completed",
            operation_id,
            {
                "operation_key": operation_id,
                "task_id": task_id,
                "id": call_id,
                "state": "committed",
                "error": {"error_type": error_type} if error_type else None,
                "content_preview": text[:2000],
                "content_truncated": len(text) > 2000,
                "child_task_id": output.get("task_id")
                if isinstance(output, dict)
                else None,
            },
        )


def project_request(value):
    """Record data loss explicitly; do not export private reasoning or credentials."""
    losses = []

    def clean(item, key="", depth=0):
        if _is_sensitive_key(key):
            losses.append("redacted")
            return "[REDACTED]"
        if depth > 16:
            losses.append("truncated")
            return "[TRUNCATED]"
        if isinstance(item, str):
            redacted = _redact_secret_text(item)
            if redacted != item:
                losses.append("redacted")
            if len(redacted) > 16000:
                losses.append("truncated")
            return redacted[:16000]
        if isinstance(item, dict):
            return {
                k: clean(v, k, depth + 1)
                for k, v in item.items()
                if k not in {"thinking", "reasoning_content", "signature"}
            }
        if isinstance(item, list):
            return [clean(v, depth=depth + 1) for v in item]
        return item

    projected = clean(value)
    return {**projected, "projection_losses": sorted(set(losses))}


def native_calls(value, task_id=""):
    """Read native ToolCallBlocks, including calls denied before tool execution."""
    if isinstance(value, dict):
        if value.get("type") == "tool_call":
            try:
                args = json.loads(value.get("input") or "{}")
            except ValueError, TypeError:
                args = {"unparseable_input": True}
            yield project_request(
                {
                    "name": value["name"],
                    "id": value["id"],
                    "args": args,
                    "task_id": task_id,
                }
            )
        else:
            for key, child in value.items():
                if key not in {"thinking", "reasoning_content"}:
                    yield from native_calls(child, task_id)
    elif isinstance(value, list):
        for child in value:
            yield from native_calls(child, task_id)


def visible_text(value):
    """Only ordinary output text; private reasoning blocks are never exported."""
    if isinstance(value, dict):
        if value.get("type") == "text":
            yield str(value.get("text", ""))
        elif value.get("type") != "thinking":
            for child in value.values():
                yield from visible_text(child)
    elif isinstance(value, list):
        for child in value:
            yield from visible_text(child)


def native_validation_results(value, task_id=""):
    """Recover explicit SDK validation refusals before governed dispatch."""
    if isinstance(value, dict):
        if value.get("type") == "tool_result":
            output = value.get("output")
            if value.get("state") == "error" and isinstance(output, str) and output.startswith("Input validation failed for tool "):
                yield project_request({"id": value["id"], "task_id": task_id,
                    "name": value["name"], "state": "rejected",
                    "error": {"error_type": "input_validation_failed"},
                    "receipt_source": "agentscope_validation",
                    "content_preview": output[:2000], "content_truncated": len(output) > 2000})
            return
        for key, child in value.items():
            if key not in {"thinking", "reasoning_content"}:
                yield from native_validation_results(child, task_id)
    elif isinstance(value, list):
        for child in value:
            yield from native_validation_results(child, task_id)


async def collect_native_snapshot(store, snapshot, owner):
    """Export completion, waiting and failed runs without modifying the source run."""
    events = await store.events(snapshot.run_id, owner)
    async with store.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(store.ops).where(store.ops.c.run_id == snapshot.run_id)
                )
            )
            .mappings()
            .all()
        )
    starts = {
        e["payload"]["operation_key"]: e["payload"]
        for e in events
        if e["payload"]["type"] == "research.operation_started"
    }
    calls = {}
    operations = []
    transcript = []
    validation_results = {}
    for row in rows:
        meta = starts.get(row["key"], {})
        result = row["result"] or {}
        if row["kind"].startswith("model:"):
            for reply in native_validation_results(result.get("agent_state", {}), meta.get("task_id", "")):
                validation_results[(reply["task_id"], reply["id"])] = {**reply, "receipt_model_operation_key": row["key"]}
            text = "\n".join(visible_text(result.get("response", {})))
            if text:
                transcript.append(
                    project_request(
                        {
                            "operation_key": row["key"],
                            "role": meta.get("agent_role"),
                            "task_id": meta.get("task_id"),
                            "content_preview": text[:2000],
                            "content_truncated": len(text) > 2000,
                        }
                    )
                )
            for call in native_calls(
                result.get("response", {}), meta.get("task_id", "")
            ):
                calls[(call.get("task_id"), call["id"])] = {
                    **call,
                    "role": meta.get("agent_role"),
                }
        outcome = result.get("outcome") or result
        error = outcome.get("error") or result.get("error_type")
        operations.append(
            {
                "key": row["key"],
                "kind": row["kind"],
                "state": row["state"],
                "task_id": meta.get("task_id"),
                "role": meta.get("agent_role"),
                "name": meta.get("tool_name"),
                "model": meta.get("model"),
                "actual": row["actual"],
                "reserved": row["reservation"] if row["state"] != "committed" else {},
                "reservation": row["reservation"],
                "error": {"error_type": error.get("error_type", "unknown")}
                if isinstance(error, dict)
                else error,
                "usage_status": result.get("usage_status", "unknown"),
                "cost_status": result.get("cost_status", "unknown"),
            }
        )
    for task, state in snapshot.agent_states.items():
        for reply in native_validation_results(state, task):
            validation_results.setdefault((task, reply["id"]), reply)
        for call in native_calls(state, task):
            # Prefer role/task dimensions recorded at invocation over a final-state fallback.
            if not any(c["id"] == call["id"] for c in calls.values()):
                calls[(task, call["id"])] = {
                    **call,
                    "role": "supervisor" if task == "supervisor" else None,
                }
    requests = [
        e for e in events if e["payload"]["type"] == "evaluation.tool_requested"
    ]
    completed = {
        (e["payload"].get("task_id"), e["payload"]["id"]): e["payload"]
        for e in events
        if e["payload"]["type"] == "evaluation.tool_completed"
    }
    by_key = {row["key"]: row for row in operations}
    for event in requests:
        request = event["payload"]
        operation = by_key.get(request["operation_key"], {})
        call_key = (request.get("task_id"), request["id"])
        # Supervisor model calls can use the stage's root scope while tools use 'supervisor'.
        if call_key not in calls:
            matching = [
                key
                for key, call in calls.items()
                if call["id"] == request["id"] and call.get("role") == "supervisor"
            ]
            for key in matching:
                calls[call_key] = calls.pop(key)
        done = completed.get(call_key, operation)
        calls[call_key] = {
            **calls.get(call_key, {}),
            **request,
            "sequence": event["sequence"],
            "task_id": request.get("task_id"),
            "state": done.get("state", "unknown"),
            "error": done.get("error"),
            "content_preview": done.get("content_preview", ""),
            "content_truncated": done.get("content_truncated", False),
            "child_task_id": done.get("child_task_id"),
        }
    for key, reply in validation_results.items():
        if key in calls and not calls[key].get("state"):
            calls[key].update(reply)
    supervisor_names = {
        "ConductResearch",
        "StartResearchTask",
        "CheckResearchTask",
        "WaitForResearchUpdates",
        "ReadResearchArtifact",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "WaitForTeamEvents",
        "TaskStop",
    }
    supervisor, researcher, results, supervisor_results = [], [], [], []
    for call in calls.values():
        is_supervisor = (
            call.get("role") == "supervisor"
            or call.get("task_id") == "supervisor"
            or call["name"] in supervisor_names
        )
        (supervisor if is_supervisor else researcher).append(call)
        if call.get("state"):
            (supervisor_results if is_supervisor else results).append(
                {
                    "name": call["name"],
                    "task_id": call.get("task_id"),
                    "tool_call_id": call["id"],
                    "status": "error" if call.get("error") else call["state"],
                    "content_preview": call.get("content_preview", ""),
                    "content_truncated": call.get("content_truncated", False),
                }
            )
    missing = [
        c["id"]
        for c in calls.values()
        if ("operation_key" not in c and c.get("receipt_source") != "agentscope_validation")
        or c.get("state") not in {"committed", "not_executed", "rejected"}
    ]
    losses = sorted(
        {loss for c in calls.values() for loss in c.get("projection_losses", [])}
    )
    capture = snapshot.application.get("evaluation_capture") is True
    completeness = (
        "complete"
        if capture and not missing and "truncated" not in losses
        else "partial"
        if calls
        else "missing"
    )
    product = snapshot.report_product
    from open_deep_research.quality.planning import unique_evidence
    evidence = unique_evidence([
        r for task in snapshot.findings for r in task.get("evidence_registry", [])
    ])
    view = build_evaluation_snapshot(
        {
            "research_brief": snapshot.research_brief,
            "coverage_checklist": product.get("coverage_checklist", []),
            "evidence_registry": evidence,
            "completed_task_outputs": snapshot.findings,
        }
    ).model_dump(mode="json")
    budget = await store.budget(snapshot.run_id, owner)
    view.update(
        schema_version="2.0",
        transcript=transcript,
        handoffs=[
            project_request(
                {
                    k: task[k]
                    for k in (
                        "task_id",
                        "research_topic",
                        "requirement_ids",
                        "compressed_research",
                        "termination",
                    )
                    if k in task
                }
            )
            for task in snapshot.findings
        ],
        operations=operations,
        events=[
            {"sequence": e["sequence"], **project_request(e["payload"])}
            for e in events
            if e["payload"]["type"]
            in {
                "research.state",
                "evaluation.tool_requested",
                "evaluation.tool_completed",
                "research.operation_committed",
            }
        ],
        outcome={
            "status": snapshot.status,
            "error": snapshot.error,
            "completion": snapshot.completion_outcome,
            "report_present": bool(snapshot.final_report),
        },
        provenance={
            "source": "native_sql",
            "run_id": snapshot.run_id,
            "projection_losses": losses,
        },
        coverage_contract=snapshot.coverage_contract,
    )
    view["tool_trace"].update(
        supervisor_tool_calls=supervisor,
        supervisor_tool_results=supervisor_results,
        researcher_tool_calls=researcher,
        researcher_tool_results=results,
        completeness=completeness,
        missing_call_ids=missing,
        run_metrics={"budget": budget},
        limits=budget.get("limits", {}),
        availability={
            "supervisor_messages_present": capture,
            "completed_task_outputs_present": bool(snapshot.findings),
            "researcher_tool_names_retained": completeness == "complete",
        },
        scope_note="Native SQL tool requests and receipts; incomplete requests never imply compliant execution.",
    )
    from .models import NativeEvaluationSnapshot

    return NativeEvaluationSnapshot.model_validate(view).model_dump(mode="json")
