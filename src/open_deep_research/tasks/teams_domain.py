"""SQL-only teams mutations; all callers share the enclosing command transaction."""

import json

from open_deep_research.tasks.team_messages import message_adapter


async def mutate_task(db, identity, kind, payload):
    run, member = identity.run_id, identity.member_id
    task_id = payload["task_id"]
    if kind == "task_claim":
        if identity.role == "lead":
            raise PermissionError("members_claim_their_own_tasks")
        row = await db.fetchrow(
            """UPDATE research_team_tasks t SET owner=$3,status='running',version=t.version+1,
               execution_mode=coalesce(t.execution_mode,m.execution_mode),
               snapshot=jsonb_set(t.snapshot,'{_team_feedback}',coalesce(m.session->'discussion','[]'::jsonb)),
               phase=CASE WHEN coalesce(t.execution_mode,m.execution_mode)='plan_approval'
                          THEN 'planning' ELSE 'executing' END
               FROM research_team_members m
               WHERE t.run_id=$1 AND t.task_id=$2 AND t.version=$4
                 AND t.status='pending' AND (t.owner IS NULL OR t.owner=$3)
                 AND m.run_id=t.run_id AND m.member_id=$3 AND m.status NOT IN ('closed','stopping','failed')
                 AND m.execution_token=$5 AND m.lease_expires>clock_timestamp()
                 AND NOT EXISTS (SELECT 1 FROM research_team_dependencies d
                     JOIN research_team_tasks b ON b.run_id=d.run_id AND b.task_id=d.blocker_id
                     WHERE d.run_id=t.run_id AND d.task_id=t.task_id
                       AND (b.status<>'completed' OR b.admission_status NOT IN ('accepted','accepted_with_caveats')))
                 AND NOT EXISTS (SELECT 1 FROM research_team_tasks a WHERE a.run_id=t.run_id
                     AND a.owner=$3 AND a.status IN ('running','waiting_for_confirmation'))
               RETURNING t.task_id,t.owner,t.version,t.phase,t.execution_mode""",
            run,
            task_id,
            member,
            payload["version"],
            payload["execution_token"],
        )
        if row is None:
            reason = await db.fetchval(
                """SELECT CASE
                WHEN NOT EXISTS (SELECT 1 FROM research_team_members WHERE run_id=$1 AND member_id=$3 AND execution_token=$4 AND lease_expires>clock_timestamp() AND status NOT IN ('closed','stopping','failed')) THEN 'member_lease_invalid'
                WHEN EXISTS (SELECT 1 FROM research_team_tasks WHERE run_id=$1 AND owner=$3 AND status IN ('running','waiting_for_confirmation')) THEN 'member_busy'
                WHEN t.owner IS NOT NULL AND t.owner<>$3 THEN 'assigned_to_another_member'
                WHEN EXISTS (SELECT 1 FROM research_team_dependencies d JOIN research_team_tasks b ON b.run_id=d.run_id AND b.task_id=d.blocker_id WHERE d.run_id=$1 AND d.task_id=$2 AND (b.status<>'completed' OR b.admission_status NOT IN ('accepted','accepted_with_caveats'))) THEN 'dependencies_blocked'
                ELSE 'version_conflict' END FROM research_team_tasks t WHERE run_id=$1 AND task_id=$2""",
                run,
                task_id,
                member,
                payload["execution_token"],
            )
            return {"claimed": False, "reason": reason or "task_not_found"}
        await db.execute(
            "UPDATE research_team_members SET current_task_id=$3,status=$4 WHERE run_id=$1 AND member_id=$2",
            run,
            member,
            task_id,
            row["phase"],
        )
        return {"claimed": True, **dict(row)}
    if identity.role != "lead":
        raise PermissionError("team_lead_required")
    if kind == "task_assign":
        target = await db.fetchval(
            """SELECT member_id FROM research_team_members WHERE run_id=$1
               AND (member_id=$2 OR name=$2) AND member_id<>'lead' AND status NOT IN ('closed','stopping')""",
            run,
            payload["owner"],
        )
        if target is None:
            raise ValueError("unknown_member")
        version = await db.fetchval(
            """UPDATE research_team_tasks SET owner=$3,version=version+1
               WHERE run_id=$1 AND task_id=$2 AND status='pending' AND version=$4 RETURNING version""",
            run,
            task_id,
            target,
            payload["version"],
        )
        if version is None:
            raise ValueError("task_version_conflict")
        return {"task_id": task_id, "owner": target, "version": version}
    if kind == "task_update":
        row = await db.fetchrow(
            """UPDATE research_team_tasks SET version=version+1 WHERE run_id=$1 AND task_id=$2
               AND status='pending' AND version=$3 RETURNING snapshot""",
            run,
            task_id,
            payload["version"],
        )
        if row is None:
            raise ValueError("task_version_conflict_or_started")
        snapshot = json.loads(row["snapshot"])
        for field, target in (
            ("subject", "display_title"),
            ("description", "research_topic"),
            ("activeForm", "activeForm"),
        ):
            if field in payload and payload[field] is not None:
                snapshot[target] = payload[field]
        await db.execute(
            "UPDATE research_team_tasks SET snapshot=$3::jsonb,metadata=metadata || $4::jsonb WHERE run_id=$1 AND task_id=$2",
            run,
            task_id,
            json.dumps(snapshot),
            json.dumps(payload.get("metadata") or {}),
        )
        return {"task_id": task_id}
    raise ValueError("unknown_task_mutation")


async def edit_dependencies(db, run, task_id, payload):
    """Graph edits serialize on the team; affected task versions defeat stale claims."""
    edges = []
    for name, reverse, remove in (
        ("addBlockedBy", False, False),
        ("addBlocks", True, False),
        ("removeBlockedBy", False, True),
        ("removeBlocks", True, True),
    ):
        for other in payload.get(name, []):
            edges.append(
                (other if reverse else task_id, task_id if reverse else other, remove)
            )
    for target, blocker, remove in sorted(set(edges)):
        found = await db.fetchval(
            """UPDATE research_team_tasks SET version=version+1 WHERE run_id=$1 AND task_id=$2
               AND status='pending' RETURNING task_id""",
            run,
            target,
        )
        if found is None:
            raise ValueError("dependency_target_missing_or_started")
        if remove:
            await db.execute(
                "DELETE FROM research_team_dependencies WHERE run_id=$1 AND task_id=$2 AND blocker_id=$3",
                run,
                target,
                blocker,
            )
            continue
        if not await db.fetchval(
            "SELECT 1 FROM research_team_tasks WHERE run_id=$1 AND task_id=$2",
            run,
            blocker,
        ):
            raise ValueError("dependency_task_not_found")
        cycle = await db.fetchval(
            """WITH RECURSIVE ancestors(id) AS (SELECT $3::text UNION
                 SELECT d.blocker_id FROM research_team_dependencies d JOIN ancestors a ON d.task_id=a.id WHERE d.run_id=$1)
               SELECT EXISTS(SELECT 1 FROM ancestors WHERE id=$2)""",
            run,
            target,
            blocker,
        )
        if cycle:
            raise ValueError("task_dependency_cycle")
        await db.execute(
            "INSERT INTO research_team_dependencies(run_id,task_id,blocker_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
            run,
            target,
            blocker,
        )


async def apply_message(db, identity, event):
    """Validate control authority before publishing; update state with the event."""
    body = event.payload.get("message")
    if isinstance(body, str):
        return
    body = message_adapter.validate_python(body).model_dump(mode="json")
    event.payload["message"] = body
    run, sender, kind = identity.run_id, identity.member_id, body["type"]
    if (
        kind in {"plan_approval_response", "shutdown_request", "task_assignment"}
        and identity.role != "lead"
    ):
        raise PermissionError("team_lead_required")
    if kind in {"evidence_share", "help_request", "task_result", "task_assignment"}:
        owner = await db.fetchrow(
            "SELECT owner FROM research_team_tasks WHERE run_id=$1 AND task_id=$2",
            run,
            body["task_id"],
        )
        if owner is None:
            raise ValueError("message_task_not_found_in_run")
        if kind == "task_assignment" and event.recipients != [owner["owner"]]:
            raise ValueError("assignment_notice_requires_current_owner")
    if kind in {"plan_approval_request", "task_proposal"} and event.recipients != [
        "lead"
    ]:
        raise ValueError("request_must_target_lead")
    if kind == "task_proposal":
        await db.execute(
            "INSERT INTO research_team_proposals(event_id,run_id,member_id,content) VALUES($1,$2,$3,$4::jsonb)",
            event.event_id,
            run,
            sender,
            json.dumps(body),
        )
    elif kind == "plan_approval_request":
        row = await db.fetchrow(
            """UPDATE research_team_tasks t SET phase='awaiting_plan_review',plan_version=plan_version+1,version=t.version+1
               FROM research_team_members m WHERE t.run_id=$1 AND t.task_id=$2 AND t.owner=$3
                 AND t.status='running' AND t.execution_mode='plan_approval' AND t.phase='planning'
                 AND m.run_id=t.run_id AND m.member_id=t.owner
               RETURNING t.plan_version,t.plan_revision_limit,t.snapshot,m.execution_epoch""",
            run,
            body["task_id"],
            sender,
        )
        if row is None:
            raise ValueError("task_not_planning_for_sender")
        if row["plan_version"] > row["plan_revision_limit"]:
            raise ValueError("plan_revision_limit_requires_human")
        if set(body["plan"]["requirement_ids"]) != set(
            json.loads(row["snapshot"])["requirement_ids"]
        ):
            raise ValueError("plan_must_preserve_assigned_requirements")
        await db.execute(
            """INSERT INTO research_team_plans(run_id,task_id,version,owner,execution_epoch,request_id,content)
               VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)""",
            run,
            body["task_id"],
            row["plan_version"],
            sender,
            row["execution_epoch"],
            event.event_id,
            json.dumps(body["plan"]),
        )
        event.payload.update(request_id=event.event_id, version=row["plan_version"])
        event.task_id, event.request_id = body["task_id"], event.event_id
        event.entity_version = row["plan_version"]
        await db.execute(
            "UPDATE research_team_members SET status='awaiting_plan_review' WHERE run_id=$1 AND member_id=$2",
            run,
            sender,
        )
    elif kind == "plan_approval_response":
        plan = await db.fetchrow(
            """UPDATE research_team_plans p SET status=$5,feedback=$6,reviewed_by='lead',reviewed_at=clock_timestamp()
               FROM research_team_tasks t, research_team_members m
               WHERE p.run_id=$1 AND p.task_id=$2 AND p.version=$3 AND p.request_id=$4 AND p.status='pending'
                 AND t.run_id=p.run_id AND t.task_id=p.task_id AND t.plan_version=p.version
                 AND t.owner=p.owner AND t.phase='awaiting_plan_review' AND t.status='running'
                 AND m.run_id=p.run_id AND m.member_id=p.owner AND m.execution_epoch=p.execution_epoch
                 AND m.lease_expires>clock_timestamp()
               RETURNING p.owner""",
            run,
            body["task_id"],
            body["version"],
            body["request_id"],
            "approved" if body["approve"] else "rejected",
            body["feedback"],
        )
        if plan is None or event.recipients != [plan["owner"]]:
            raise ValueError("stale_plan_or_wrong_recipient")
        limit = await db.fetchval(
            "SELECT plan_revision_limit FROM research_team_tasks WHERE run_id=$1 AND task_id=$2",
            run,
            body["task_id"],
        )
        await db.execute(
            "UPDATE research_team_tasks SET phase=$3,version=version+1 WHERE run_id=$1 AND task_id=$2",
            run,
            body["task_id"],
            "executing"
            if body["approve"]
            else "awaiting_human"
            if body["version"] >= limit
            else "planning",
        )
        await db.execute(
            """UPDATE research_team_members m SET status=t.phase FROM research_team_tasks t
               WHERE m.run_id=$1 AND m.member_id=$2 AND t.run_id=m.run_id AND t.task_id=$3""",
            run,
            plan["owner"],
            body["task_id"],
        )
    elif kind == "shutdown_request":
        await db.execute(
            """UPDATE research_team_members SET status='stopping',session=session || $3::jsonb
               WHERE run_id=$1 AND member_id=ANY($2::text[]) AND member_id<>'lead'""",
            run,
            event.recipients,
            json.dumps({"shutdown_request": event.event_id}),
        )
    elif kind == "shutdown_response":
        valid = await db.fetchval(
            "SELECT 1 FROM research_team_members WHERE run_id=$1 AND member_id=$2 AND session->>'shutdown_request'=$3",
            run,
            sender,
            body["request_id"],
        )
        if not valid:
            raise ValueError("shutdown_request_not_current")
        if not body["approve"]:
            await db.execute(
                """UPDATE research_team_members SET status='idle',
                session=(session-'shutdown_request') || $3::jsonb WHERE run_id=$1 AND member_id=$2""",
                run,
                sender,
                json.dumps({"shutdown_declined": body["reason"]}),
            )
