"""Real provider comparison in the Gateway container, with governed SQL receipts.

The CLI owns an isolated evaluation journal. Its explicit network authorizer
permits only the selected search service endpoints, never arbitrary result URLs.
It does not change deployment policy or resolve browser approval requests.
"""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.agentscope_runtime.search_providers import SearchResources
from open_deep_research.agentscope_runtime.tools import prepare_toolkit
from open_deep_research.agentscope_runtime.web_tools import source_discovery_tool
from open_deep_research.configuration import freeze_run_config
from open_deep_research.evaluation.trace import EvaluationRecorder
from open_deep_research.sandbox.egress_context import egress_authorizer
from open_deep_research.tools.base import ToolExecutionZone
from open_deep_research.tools.governance import AgentRole


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    source = json.loads(args.source.read_text(encoding="utf8"))
    source_contract = json.loads(args.contract.read_text(encoding="utf8"))
    queries = json.loads(args.queries.read_text(encoding="utf8"))
    from open_deep_research.models.resolution import resolve_named_api_key
    runtime_credentials = {"configurable": {"apiKeys": {"TAVILY_API_KEY": os.environ.get("TAVILY_API_KEY", "")}}}
    if not resolve_named_api_key("TAVILY_API_KEY", runtime_credentials):
        raise ValueError("comparison_missing_tavily_credential")
    assert len(queries) == 12 and len(set(queries)) == 12
    store = RecoveryStore("sqlite+aiosqlite:///" + (args.output.resolve() / "comparison.db").as_posix())
    await store.create_tables()
    resources = SearchResources()
    sessions, toolkits, renewals, outputs = {}, {}, [], []

    async def renew(session):
        while True:
            await asyncio.sleep(20)
            await store.renew(session.lease, 120)

    async def authorize(url, capability, consume=False):
        target = urlsplit(url)
        allowed = target.scheme == "https" and target.port in {None, 443} and (
            target.hostname == "api.tavily.com" and target.path == "/search"
            or target.hostname in {"www.bing.com", "cn.bing.com"} and target.path == "/search")
        return "allow" if allowed and capability == "search.provider" else "deny"

    token = egress_authorizer.set(authorize)
    try:
        for policy in ("tavily", "bing", "dual"):
            values = dict(source["contract"]["configurable"])
            values.update(search_providers=["tavily", "bing"] if policy == "dual" else [policy],
                max_run_tool_calls=200, max_run_model_calls=1, run_deadline_seconds=1800,
                enable_memory=False, quality_evaluation_enabled=False,
                # The CLI already runs inside the read-only Gateway container;
                # it does not create a nested sandbox or need the host root key.
                sandbox_enabled=False)
            frozen = freeze_run_config({"configurable": values}, prefer_configurable=True)
            run = RunConfig.compile(frozen)
            run_id = "search-comparison-" + policy + "-" + uuid4().hex
            await store.create_from_config("offline-search-comparison", run_id, run,
                application={"purpose": "provider_comparison", "evaluation_capture": True})
            session = await RecoverySession.open(store, run_id, "offline-search-comparison", ttl=120)
            sessions[policy] = session
            renewals.append(asyncio.create_task(renew(session)))
            config = run.compatibility_projection()
            config["metadata"].update(run_id=run_id, task_id="source-planning", coverage_contract=source_contract,
                source_selection=source_contract.get("source_selection", {"mode": "web", "sources": []}))
            config["configurable"]["runs_dir"] = str(args.output)
            config["configurable"]["apiKeys"] = runtime_credentials["configurable"]["apiKeys"]
            config["_evaluation_recorder"] = EvaluationRecorder(session)
            toolkit = await prepare_toolkit([source_discovery_tool(lambda config=config: config, None, resources=resources)],
                role=AgentRole.RESEARCHER, config_provider=lambda config=config: config,
                run_id=run_id, task_id="source-planning", local_zones=frozenset({ToolExecutionZone.GATEWAY}))
            toolkit.journal = session
            toolkits[policy] = toolkit
        for repeat in range(1, 4):
            for index, query in enumerate(queries):
                policies = ("tavily", "bing", "dual") if (repeat + index) % 2 else ("dual", "bing", "tavily")
                for policy in policies:
                    session, toolkit = sessions[policy], toolkits[policy]
                    payloads = []
                    async def observe(name, call_id, result, payloads=payloads):
                        payloads.append({"output": result.result.output if result.result else None,
                            "error": result.error.model_dump(mode="json") if result.error else None})
                    toolkit.result_observer = observe
                    started = time.monotonic()
                    with session.scope("search_comparison", 0), session.task("source-planning"):
                        await toolkit.call_domain_tool("source_discovery", {"queries": [query]}, f"query-{index}-repeat-{repeat}")
                    row = {"policy": policy, "repeat": repeat, "query_index": index, "query": query,
                        "seconds": time.monotonic() - started, **payloads[-1]}
                    outputs.append(row)
                    (args.output / "results.json").write_text(json.dumps(outputs, ensure_ascii=False, indent=2), encoding="utf8")
                    print(json.dumps({k: row[k] for k in ("policy", "repeat", "query_index", "seconds", "error")}), flush=True)
        for policy, session in sessions.items():
            (args.output / (policy + "-budget.json")).write_text(json.dumps(await store.budget(session.lease.run_id,
                session.lease.user_id), indent=2), encoding="utf8")
    finally:
        egress_authorizer.reset(token)
        for task in renewals:
            task.cancel()
        await asyncio.gather(*renewals, return_exceptions=True)
        for session in sessions.values():
            await session.close()
        await resources.aclose()
        await store.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
