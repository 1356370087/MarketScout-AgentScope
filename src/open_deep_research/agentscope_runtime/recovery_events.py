"""Project durable M6 facts into the existing public event vocabulary."""

_STAGES = {
    "summarize_messages": "preparing",
    "memory_recall": "preparing",
    "clarify_with_user": "planning",
    "write_research_brief": "planning",
    "plan_approval": "planning",
    "research_supervisor": "researching",
    "outline_approval": "synthesizing",
    "final_report_generation": "writing",
    "memory_extract_and_write": "finalizing",
}
_PUBLIC_STAGES = tuple(dict.fromkeys(_STAGES.values()))


def public_events(event):
    payload = event["payload"]
    if payload.get("type", "").startswith("evaluation."):
        return []  # Local evaluation payloads never enter public SSE or telemetry.
    output = []
    decision = payload.get("decision")
    if decision and decision["action"] != "feedback":
        kind = decision.get("kind") or "plan_approval"
        if kind in {"tool", "egress"}:
            output.append(
                (
                    "security.approval.resolved",
                    None,
                    {
                        "approval_id": decision["action_id"],
                        "kind": kind,
                        "decision": decision["action"],
                        "status": "resolved",
                    },
                )
            )
        else:
            output.append(
                (
                    "clarification.resolved"
                    if kind == "clarify_with_user"
                    else "approval.resolved",
                    None,
                    {
                        "action_id": decision["action_id"],
                        "approval_type": kind.removesuffix("_approval"),
                        "action": decision["action"],
                        "status": "resolved",
                    },
                )
            )
    for action_id, approval in sorted(payload.get("approvals", {}).items()):
        if approval["kind"] == "budget":
            output.append(
                (
                    "approval.required",
                    None,
                    {
                        "action_id": action_id,
                        "approval_type": "budget",
                        "status": "pending",
                        "allowed_actions": ["approve", "cancel"],
                    },
                )
            )
        else:
            detail = approval["payload"].get("detail", {})
            output.append(
                (
                    "security.approval.required",
                    None,
                    {
                        "approval_id": action_id,
                        "kind": approval["kind"],
                        "target": detail.get("domain")
                        or approval["payload"].get("tool_name"),
                        "status": "pending",
                    },
                )
            )
    mapped = public_event(event)
    if mapped and not (payload.get("approvals") and payload.get("status") == "waiting"):
        output.append(mapped)
    return output


def public_event(event):
    payload = event["payload"]
    kind = payload["type"]
    if kind == "research.public":
        return payload["public_type"], payload.get("stage"), payload["public_payload"]
    if kind == "research.progress":
        return "research.progress.updated", "researching", {"progress": payload["progress"]}
    if kind == "research.cancelled":
        return (
            "run.cancelled",
            None,
            {"status": "cancelled", "termination_reason": "user_cancelled"},
        )
    if kind in {"research.operation_committed", "research.usage_reconciled"}:
        return (
            "run.usage.updated",
            None,
            {"revision": event["sequence"], "accounting_status": "committed"},
        )
    if kind != "research.state":
        return None
    status = payload["status"]
    if status in {"completed", "failed", "cancelled"}:
        return (
            "run." + status,
            "finalizing",
            {
                "status": status,
                "result_status": payload.get("result_status"),
                "error_code": payload.get("error_code"),
            },
        )
    pending = payload.get("pending")
    if pending:
        if pending["stage"] == "clarify_with_user":
            return (
                "clarification.required",
                "planning",
                {
                    "action_id": pending["id"],
                    "question": pending["question"],
                    "status": "pending",
                    "allowed_actions": ["answer", "cancel"],
                },
            )
        return (
            "approval.required",
            _STAGES[pending["stage"]],
            {
                "action_id": pending["id"],
                "approval_type": pending["stage"].removesuffix("_approval"),
                "status": "pending",
                "content_markdown": pending["question"],
                "allowed_actions": ["approve", "revise", "cancel"],
            },
        )
    stage = payload.get("stage")
    if stage:
        return (
            "stage.started",
            _STAGES.get(stage),
            {
                "stage_id": _STAGES[stage],
                "stage_index": _PUBLIC_STAGES.index(_STAGES[stage]),
                "stage_count": len(_PUBLIC_STAGES),
            },
        )
    if payload.get("completed"):
        stage = payload["completed"][-1]
        return (
            "stage.completed",
            _STAGES.get(stage),
            {
                "stage_id": _STAGES[stage],
                "stage_index": _PUBLIC_STAGES.index(_STAGES[stage]),
                "stage_count": len(_PUBLIC_STAGES),
            },
        )
    return "run.started", "preparing", {"status": status}
