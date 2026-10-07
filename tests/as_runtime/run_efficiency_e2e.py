"""Real HTTP research comparisons; never substitute synthetic search/model results."""

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

URLS = [
    "https://www.postgresql.org/docs/17/" + page
    for page in (
        "app-pgbasebackup.html",
        "app-pgcombinebackup.html",
        "continuous-archiving.html",
    )
]
QUESTIONS = {
    "complex": "根据所选的 PostgreSQL 17 官方页面，说明增量备份的生成与合并恢复流程，并明确证据支持的前提条件和限制。报告使用中文。",
    "simple": "使用一个研究任务。\n仅依据所选页面，回答 PostgreSQL 的增量备份能否直接使用，以及 pg_combinebackup 在恢复前的作用。\n报告使用中文。",
    "web": "仅使用 PostgreSQL 官方文档，核实 PostgreSQL 17 的增量备份支持：说明如何生成增量备份、恢复前如何合并、与常规基础备份的关系和主要限制。至少引用3个不同的官方文档页面并给出可核验链接。报告使用中文，控制在1000字左右，不需要性能跑分或市场分析。",
}
PUBLIC_TEST_DOMAINS = {
    "www.postgresql.org",
    "postgresql.org",
    "api.tavily.com",
    "www.bing.com",
}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_case(client, args, case, policy, repetition):
    selection = (
        {"mode": "web"}
        if case == "web"
        else {"mode": "specific", "sources": [{"type": "url", "url": u} for u in URLS]}
    )
    response = await client.post(
        "/runs",
        json={
            "messages": [{"role": "user", "content": QUESTIONS[case]}],
            "source_selection": selection,
            "configurable": {
                "allow_clarification": False,
                "enable_human_in_loop": True,
                "enable_async_research": False,
                "research_efficiency_mode": policy,
                "run_deadline_seconds": args.deadline,
                "quality_evaluation_enabled": True,
                "quality_evaluation_fail_open": False,
                "report_review_enabled": case != "web",
                "report_review_fail_open": False,
            },
        },
    )
    response.raise_for_status()
    run_id = response.json()["run_id"]
    directory = args.output / f"{case}-{policy}-{repetition}-{run_id}"
    directory.mkdir(parents=True)
    save(
        directory / "request.json",
        {
            "case": case,
            "policy": policy,
            "question": QUESTIONS[case],
            "selection": selection,
        },
    )
    print(
        json.dumps(
            {"event": "started", "case": case, "policy": policy, "run_id": run_id}
        ),
        flush=True,
    )
    started = time.monotonic()
    prior = None
    final = None
    try:
        while time.monotonic() - started < args.deadline + 300:
            current = await client.get(f"/runs/{run_id}")
            current.raise_for_status()
            final = current.json()
            state = (
                final["status"],
                final["progress"].get("current_stage"),
                final["progress"].get("tasks", {}).get("total"),
            )
            if state != prior:
                print(
                    json.dumps(
                        {
                            "event": "progress",
                            "run_id": run_id,
                            "state": state,
                            "elapsed_seconds": round(time.monotonic() - started),
                        }
                    ),
                    flush=True,
                )
                prior = state
            save(directory / "snapshot.json", final)
            if final["status"] in {"completed", "failed", "cancelled"}:
                break
            action = final.get("pending_human_action")
            if action and final["status"] in {
                "awaiting_plan_approval",
                "awaiting_outline_approval",
            }:
                reply = await client.post(
                    f"/runs/{run_id}/human-actions/{action['action_id']}",
                    json={"action": "approve"},
                )
                reply.raise_for_status()
            for approval in final.get("pending_security_approvals", []):
                domain = approval.get("target", {}).get("domain")
                decision = "allow_run" if domain in PUBLIC_TEST_DOMAINS else "deny"
                reply = await client.post(
                    f"/runs/{run_id}/security-approvals/{approval['approval_id']}",
                    json={"decision": decision},
                )
                if reply.status_code not in {200, 409}:
                    reply.raise_for_status()
            await asyncio.sleep(3)
        if final is None or final["status"] not in {"completed", "failed", "cancelled"}:
            raise TimeoutError("e2e_observation_timeout")
    finally:
        if final is None or final["status"] not in {"completed", "failed", "cancelled"}:
            await client.post(f"/runs/{run_id}/cancel")
    usage_response = await client.get(f"/runs/{run_id}/usage")
    usage_response.raise_for_status()
    usage = usage_response.json()
    save(directory / "usage.json", usage)
    for task_id in final["progress"].get("task_items", {}):
        activity = await client.get(
            f"/runs/{run_id}/tasks/{task_id}/activity", params={"limit": 200}
        )
        activity.raise_for_status()
        save(directory / f"activity-{task_id}.json", activity.json())
    output = final.get("output") or {}
    report = output.get("markdown") or ""
    (directory / "report.md").write_text(report, encoding="utf-8")
    published = False
    if final["status"] == "completed" and report:
        publication = await client.post(
            f"/runs/{run_id}/publications", json={"format": "markdown"}
        )
        publication.raise_for_status()
        job = publication.json()
        for _ in range(60):
            result = await client.get(
                f"/runs/{run_id}/publications/{job['publication_id']}"
            )
            result.raise_for_status()
            if result.json()["status"] in {"completed", "failed"}:
                published = result.json()["status"] == "completed"
                break
            await asyncio.sleep(2)
        if published:
            download = await client.get(
                f"/runs/{run_id}/publications/{job['publication_id']}/download"
            )
            download.raise_for_status()
            assert len(download.content) > 100
    result = {
        "case": case,
        "policy": policy,
        "repetition": repetition,
        "run_id": run_id,
        "status": final["status"],
        "completion_status": output.get("completion_status"),
        "stop_reason": output.get("stop_reason"),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "model_calls": usage["totals"]["calls"]["attempts"],
        "input_tokens": usage["totals"]["reported"]["input_tokens"],
        "output_tokens": usage["totals"]["reported"]["output_tokens"],
        "cost_micro_usd": usage["totals"]["cost"]["estimated_cost_micro_usd"],
        "source_count": len(final["progress"].get("sources", [])),
        "publication_completed": published,
        "report_review": output.get("report_review"),
        "cache_reporting": usage.get("cache_reporting"),
        "efficiency": usage.get("efficiency"),
        "source_versions": usage.get("research_progress", {}).get(
            "source_versions", {}
        ),
    }
    review = output.get("report_review") or {}
    result["research_usable"] = (
        final["status"] == "completed" and result["source_count"] > 0 and published
    )
    result["quality_passed"] = case == "web" or (
        review.get("decision") == "pass" and not review.get("degraded")
    )
    result["usable"] = result["research_usable"] and result["quality_passed"]
    result["accounting_complete"] = usage.get("accounting_status") == "complete"
    result["cost_source"] = usage["totals"]["cost"].get("cost_source")
    save(directory / "result.json", result)
    print(json.dumps({"event": "finished", **result}, ensure_ascii=False), flush=True)
    return result


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/") + "/", timeout=60, trust_env=False
    ) as client:
        for case in args.cases:
            for repetition in range(1, args.repeats + 1):
                for policy in (
                    args.policies if repetition % 2 else reversed(args.policies)
                ):
                    result = await run_case(client, args, case, policy, repetition)
                    results.append(result)
                    save(args.output / "results.json", results)
    summary = {}
    for case in args.cases:
        for policy in args.policies:
            rows = [r for r in results if r["case"] == case and r["policy"] == policy]
            summary[f"{case}:{policy}"] = {
                "samples": len(rows),
                "usable": sum(r["usable"] for r in rows),
                **{
                    name: {
                        "median": statistics.median(r[name] for r in rows),
                        "min": min(r[name] for r in rows),
                        "max": max(r[name] for r in rows),
                    }
                    for name in (
                        "model_calls",
                        "input_tokens",
                        "output_tokens",
                        "elapsed_seconds",
                        "cost_micro_usd",
                    )
                },
            }
    save(args.output / "summary.json", summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="仅在已授权的专用部署上执行真实研究与模型费用对照。"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8088/api/research")
    parser.add_argument(
        "--output", type=Path, default=Path("output/research-efficiency/live")
    )
    parser.add_argument(
        "--cases", nargs="+", choices=list(QUESTIONS), default=list(QUESTIONS)
    )
    parser.add_argument(
        "--policies",
        nargs="+",
        choices=["baseline", "bounded"],
        default=["baseline", "bounded"],
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--deadline", type=int, default=3600)
    asyncio.run(main(parser.parse_args()))
