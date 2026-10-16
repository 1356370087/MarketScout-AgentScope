"""Real authenticated multi-topic research acceptance; preserves failed samples.

The reference facts are for independent inspection, never injected into the
researcher's prompt. No model score substitutes for those content checks.
"""

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class AuthenticatedClient:
    def __init__(self, client, account):
        self.client, self.account = client, account
        self.refresh_token = None
        self.expiry = 0

    async def authenticate(self):
        if self.refresh_token:
            response = await self.client.post("/auth/refresh", json={"refresh_token": self.refresh_token})
        else:
            response = await self.client.post("/auth/login", json={k: self.account[k] for k in ("email", "password")})
        response.raise_for_status()
        pair = response.json()
        self.client.headers["Authorization"] = "Bearer " + pair["access_token"]
        self.refresh_token = pair["refresh_token"]
        self.expiry = time.monotonic() + pair["expires_in"] - 45

    async def request(self, method, path, **kwargs):
        if time.monotonic() >= self.expiry:
            await self.authenticate()
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response


async def run_case(client, case, args, implementation, budget):
    directory = args.output / case["id"]
    directory.mkdir(exist_ok=True)
    request = {"messages": [{"role": "user", "content": case["question"]}],
        "source_selection": case["selection"], "configurable": {
            "enable_async_research": False, "enable_human_in_loop": True, "allow_clarification": False,
            "report_review_enabled": True, "report_review_fail_open": False,
            "quality_evaluation_enabled": True, "quality_evaluation_fail_open": False,
            "run_deadline_seconds": args.deadline, "max_run_cost_micro_usd": budget}}
    identity_file = directory / "run.json"
    resumed = identity_file.exists()
    if resumed:
        prior = json.loads(identity_file.read_text(encoding="utf-8"))
        if prior["request"] != request or prior["implementation"] != implementation:
            raise ValueError("Existing trial differs; use a new output directory.")
        run_id = prior["run_id"]
    else:
        created = await client.request("POST", "/runs", json=request)
        run_id = created.json()["run_id"]
        save(identity_file, {"run_id": run_id, "request": request, "implementation": implementation})
    print(json.dumps({"event": "started", "case": case["id"], "run_id": run_id}), flush=True)
    started, previous, final = time.monotonic(), None, None
    allowed = set(case["allowed_domains"]) | {"api.tavily.com", "www.bing.com"}
    approvals_path = directory / "approvals.json"
    approvals = json.loads(approvals_path.read_text(encoding="utf-8")) if approvals_path.exists() else []
    try:
        while time.monotonic() - started < args.deadline + 180:
            final = (await client.request("GET", f"/runs/{run_id}")).json()
            state = (final["status"], final["progress"].get("current_stage"), final["progress"].get("tasks"))
            save(directory / "snapshot.json", final)
            if state != previous:
                print(json.dumps({"event": "progress", "case": case["id"], "state": state}, ensure_ascii=False), flush=True)
                previous = state
            if final["status"] in {"completed", "failed", "cancelled"}:
                break
            action = final.get("pending_human_action")
            if action and final["status"] in {"awaiting_plan_approval", "awaiting_outline_approval"}:
                await client.request("POST", f"/runs/{run_id}/human-actions/{action['action_id']}", json={"action": "approve"})
                approvals.append({"type": "research_plan_or_outline", "action_id": action["action_id"], "decision": "approve"})
            for approval in final.get("pending_security_approvals", []):
                target = approval.get("target", {})
                domain = target.get("domain") or urlsplit(target.get("url", "")).hostname
                decision = "allow_once" if domain in allowed else "deny"
                await client.request("POST", f"/runs/{run_id}/security-approvals/{approval['approval_id']}", json={"decision": decision})
                approvals.append({"type": "network", "domain": domain, "decision": decision})
            await asyncio.sleep(3)
        else:
            raise TimeoutError("research_observation_timeout")
    finally:
        save(directory / "approvals.json", approvals)
        if final is None or final["status"] not in {"completed", "failed", "cancelled"}:
            await client.request("POST", f"/runs/{run_id}/cancel")
    usage = (await client.request("GET", f"/runs/{run_id}/usage")).json()
    save(directory / "usage.json", usage)
    for task_id in final["progress"].get("task_items", {}):
        response = await client.request("GET", f"/runs/{run_id}/tasks/{task_id}/activity", params={"limit": 200})
        save(directory / f"activity-{task_id}.json", response.json())
    output = final.get("output") or {}
    report = output.get("markdown") or ""
    (directory / "report.md").write_text(report, encoding="utf-8")
    save(directory / "independent-review-input.json", {"required_facts": case["required_facts"],
        "negative_controls": case["negative_controls"], "reference_sources": case["reference_sources"],
        "status": "pending_independent_inspection"})
    published = False
    publication_errors = []
    if report and final["status"] == "completed":
        publication = None
        for attempt in range(10):
            try:
                publication = (await client.request("POST", f"/runs/{run_id}/publications", json={"format": "markdown"})).json()
                break
            except httpx.HTTPStatusError as exc:
                detail = exc.response.json().get("detail")
                publication_errors.append({"status": exc.response.status_code, "detail": detail})
                save(directory / "publication-errors.json", publication_errors)
                if exc.response.status_code != 409 or detail not in {"run already owned or not visible", "run_not_completed"} or attempt == 9:
                    break
                # Completed checkpoint can precede the executor's lease release.
                # Publication enqueue is idempotent; retain each transient conflict.
                await asyncio.sleep(2)
        if publication:
            for _ in range(40):
                publication = (await client.request("GET", f"/runs/{run_id}/publications/{publication['publication_id']}")).json()
                if publication["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(2)
            save(directory / "publication.json", publication)
            if publication["status"] == "completed":
                download = await client.request("GET", f"/runs/{run_id}/publications/{publication['publication_id']}/download")
                (directory / "downloaded.md").write_bytes(download.content)
                published = len(download.content) > 100
    review = output.get("report_review") or {}
    budgets = usage["totals"].get("budgets", {})
    result = {"case": case["id"], "run_id": run_id, "status": final["status"],
        "completion_status": output.get("completion_status"), "stop_reason": output.get("stop_reason"),
        "elapsed_seconds": None if resumed else round(time.monotonic()-started, 2), "review": review,
        "observation_resumed": resumed, "publication_errors": publication_errors,
        "model_calls": usage["totals"]["calls"]["attempts"],
        "cost_micro_usd": budgets.get("cost_micro_usd", {}).get("settled", 0),
        "reserved_micro_usd": budgets.get("cost_micro_usd", {}).get("reserved", 0),
        "published": published, "independent_content_review": "pending",
        "runtime_quality_passed": final["status"] == "completed" and output.get("completion_status") == "success"
            and not output.get("uncovered_requirements") and not output.get("research_gaps")
            and review.get("decision") == "pass" and not review.get("degraded") and not review.get("skipped")}
    save(directory / "result.json", result)
    print(json.dumps({"event": "finished", **result}, ensure_ascii=False), flush=True)
    return result


async def main(args):
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = [case for case in dataset["cases"] if not args.cases or case["id"] in args.cases]
    if not cases:
        raise ValueError("No matching cases")
    repo = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256(args.dataset.read_bytes())
    for path in sorted((repo / "src").rglob("*.py")):
        digest.update(path.relative_to(repo).as_posix().encode())
        digest.update(path.read_bytes())
    implementation = digest.hexdigest()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8"))["implementation"] != implementation:
        raise ValueError("Existing implementation differs; use a new output directory.")
    save(manifest_path, {"dataset": str(args.dataset), "implementation": implementation,
        "cases": [case["id"] for case in cases], "mode": "real_models_and_web", "base_url": args.base_url})
    account = json.loads(args.credentials_file.read_text(encoding="utf-8"))
    results = []
    async with httpx.AsyncClient(base_url=args.base_url, trust_env=False, timeout=90) as http:
        client = AuthenticatedClient(http, account)
        capabilities = (await client.request("GET", "/capabilities")).json()
        if not capabilities.get("runtime_ready"):
            raise RuntimeError("Research runtime is not ready")
        for case in cases:
            remaining = args.total_budget_micro_usd - sum(row["cost_micro_usd"] + row["reserved_micro_usd"] for row in results)
            if remaining <= 0:
                raise RuntimeError("Experiment model budget exhausted")
            results.append(await run_case(client, case, args, implementation, min(args.run_budget_micro_usd, remaining)))
            save(args.output / "results.json", results)
    return 0 if all(row["runtime_quality_passed"] and row["published"] and row["reserved_micro_usd"] == 0 for row in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="真实模型与联网资料的多主题研究质量验收；保留失败和降级样本。")
    parser.add_argument("--base-url", default="http://127.0.0.1:20249")
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path("tests/fixtures/research_quality/topics.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--deadline", type=int, default=1200)
    parser.add_argument("--run-budget-micro-usd", type=int, default=1000000)
    parser.add_argument("--total-budget-micro-usd", type=int, default=3000000)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
