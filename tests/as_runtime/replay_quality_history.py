"""Re-evaluate exported task receipts without changing historical run state.

Run inside the configured API container with paths to a task snapshot and JSONL
operation receipts. Uses the deployment's real LiteLLM Judge, not a mock score.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from openai import AsyncOpenAI

from open_deep_research.agentscope_runtime.research_agents import ResearchAssignment, _Observations, _compression_context
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
from open_deep_research.tools.base import ToolResult


class LiveJudge:
    def __init__(self, client):
        self.client = client
        self.calls = 0

    async def structured(self, role, prompt, schema, state):
        messages = [{"role": "user", "content": prompt}]
        for attempt in range(3):
            self.calls += 1
            response = await self.client.chat.completions.create(
                model="if-quality-v1", max_tokens=4096, messages=messages,
                tools=[{"type": "function", "function": {
                    "name": "__insightforge_structured_output", "description": "Return the required quality assessment",
                    "parameters": schema.model_json_schema(),
                }}],
                tool_choice={"type": "function", "function": {"name": "__insightforge_structured_output"}},
            )
            try:
                return schema.model_validate_json(response.choices[0].message.tool_calls[0].function.arguments)
            except (ValueError, TypeError, IndexError) as exc:
                if attempt == 2:
                    raise
                messages.append({"role": "user", "content": "Correct the structured output: " + str(exc)[:1500]})


async def main(task_path, receipts_path):
    task = json.loads(Path(task_path).read_text(encoding="utf-8-sig"))["task"]
    records = [json.loads(line) for line in Path(receipts_path).read_text(encoding="utf-8-sig").splitlines()]
    assignment = ResearchAssignment(**{key: task[key] for key in ("task_id", "research_topic", "requirement_ids")})
    config = {"configurable": {
        "quality_evaluation_enabled": True, "quality_evaluation_model": "if-quality-v1",
        "quality_evaluation_rigor": "balanced", "quality_evaluation_min_sources": 3,
        "quality_evaluation_fail_open": True, "max_structured_output_retries": 3,
    }, "metadata": {}}
    async with AsyncOpenAI(base_url="http://litellm-proxy:4000/v1", api_key=os.environ["LITELLM_MASTER_KEY"],
                           timeout=120, max_retries=0) as client:
        judge = LiveJudge(client)
        observations = _Observations(quality=NativeResearchQuality(judge, lambda: config),
                                     assignment=assignment, contract=task["coverage_contract"])
        tool_count = 0
        for record in records:
            result = record["result"]
            if ":tool:" not in record["key"] or result["message"].get("name") != "fetch_url":
                continue
            outcome = SimpleNamespace(error=result["error"], result=ToolResult(output=result["output"]),
                                      message=SimpleNamespace(content=result["message"]["content"]))
            await observations.capture("fetch_url", record["key"], outcome)
            tool_count += 1
        candidates_before = len(observations.evidence)
        assessment = await observations.assess_pending()
        urls = {row["source_url"].rstrip("/") for row in observations.evidence.values()}
        compression_input = _compression_context(observations.results, urls)
        summary = {"historical_task": task["task_id"], "historical_status": task["status"],
                   "tool_receipts": tool_count, "candidate_count": candidates_before,
                   "retained_candidate_count": len(observations.evidence), "quality_batches": len(observations.assessments),
                   "physical_judge_calls": judge.calls, "source_count": assessment["deterministic_checks"]["source_count"],
                   "decision": assessment["decision"], "accepted": assessment["accepted"],
                   "evaluator_error": assessment["evaluator_error"], "reason": assessment["reason"],
                   "failed_retry_url_removed": "https://rocketmq.apache.org/docs/retry/" not in compression_input}
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        assert tool_count >= 3 and summary["source_count"] >= 3
        assert summary["quality_batches"] == 1 and candidates_before == len(observations.evidence)
        assert assessment["accepted"] and not assessment["evaluator_error"]


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
