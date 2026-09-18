"""Plan-only model call followed by an explicit, versioned Lead decision."""

import asyncio
import json

from open_deep_research.tasks.team_messages import ResearchPlan
from open_deep_research.tasks.team_protocol import MemberIdentity


async def wait_for_plan(worker, researcher, snapshot):
    team, task_id = worker.team, worker.task_id
    identity = MemberIdentity(
        run_id=team.lease.run_id, member_id=worker.member_id, name=worker.member_id
    )
    while True:
        await worker.consume_inputs()
        async with team.transport.store.pool.acquire() as db, db.transaction():
            await worker.guard(db)
            row = await db.fetchrow(
                "SELECT phase,plan_version FROM research_team_tasks WHERE run_id=$1 AND task_id=$2",
                team.lease.run_id,
                task_id,
            )
            plans = await db.fetch(
                "SELECT version,content,status,feedback FROM research_team_plans WHERE run_id=$1 AND task_id=$2 ORDER BY version",
                team.lease.run_id,
                task_id,
            )
        if row["phase"] == "executing":
            approved = next(p for p in plans if p["version"] == row["plan_version"])
            return {
                "version": approved["version"],
                "plan": json.loads(approved["content"]),
                "lead_feedback": approved["feedback"],
            }
        if row["phase"] == "planning":
            revision = row["plan_version"] + 1
            with worker.session.scope("member-plan:" + task_id, revision):
                prompt = json.dumps(
                    {
                        "instruction": "你是成员，不是 Lead。仅规划当前 task 与 requirement_ids 对应的研究，不建队、不派发任务、不规划整个团队或最终报告。生成计划期间只读取已有材料，不执行搜索或抓取；计划应描述获批后实际执行的步骤，可列出获批后需要的搜索、抓取工具与来源，不要误写为整个研究禁止搜索。明确步骤、拟使用来源和工具、预算估算及验收标准。计划会交由 Lead 审核。requirement_ids 由运行时绑定，不可扩大职责。",
                        "task": snapshot["research_topic"],
                        "requirement_ids": snapshot["requirement_ids"],
                        "requirements": [
                            item
                            for item in snapshot["coverage_contract"].get(
                                "requirements", []
                            )
                            if item.get("requirement_id") in snapshot["requirement_ids"]
                        ],
                        "feedback": worker.session.snapshot.feedback_by_task.get(
                            task_id, []
                        ),
                        "previous_plans": [dict(p) for p in plans],
                    },
                    ensure_ascii=False,
                    default=str,
                )
                plan = await researcher.models.structured(
                    "researcher", prompt, ResearchPlan, {}
                )
                # Ownership is trusted assignment data, never a model-selected scope.
                plan.requirement_ids = list(snapshot["requirement_ids"])
                await team.send_message(
                    identity,
                    f"plan:{task_id}:{revision}",
                    "lead",
                    {
                        "type": "plan_approval_request",
                        "task_id": task_id,
                        "plan": plan.model_dump(mode="json"),
                    },
                )
        # Pending and escalated plans never auto-approve or spend polling tokens.
        await worker.consume_inputs()
        await asyncio.sleep(0.5)
