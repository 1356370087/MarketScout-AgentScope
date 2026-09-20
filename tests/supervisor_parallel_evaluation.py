import asyncio
import uuid

from pathlib import Path
from open_deep_research.evaluation.local_runtime import run_native_question
from tests.run_local_evaluate import evaluation_runtime_environment

dataset_name = "ODR: First Supervisor Parallelism"
def right_parallelism_evaluator(
    outputs: dict,
    reference_outputs: dict,
) -> dict:
    state = outputs.get("output", outputs)
    actual_parallelism = 0
    for message in state.get("supervisor_messages", []):
        value = message.model_dump(mode="json") if hasattr(message, "model_dump") else message
        if not isinstance(value, dict):
            continue
        blocks = value.get("content")
        if isinstance(blocks, list) and any(block.get("type") == "tool_call" for block in blocks if isinstance(block, dict)):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result" and actual_parallelism:
                    break
                if block.get("type") == "tool_call" and block.get("name") in {"ConductResearch", "StartResearchTask"}:
                    actual_parallelism += 1
        else:
            actual_parallelism = sum(call.get("name") in {"ConductResearch", "StartResearchTask"}
                                     for call in value.get("tool_calls", []) if isinstance(call, dict))
        if actual_parallelism:
            break
    return {
        "key": "right_parallelism",
        "score": actual_parallelism == reference_outputs["parallel"],
        "comment": (
            f"Expected {reference_outputs['parallel']} first-wave research calls; "
            f"observed {actual_parallelism}."
        ),
    }

async def target(inputs: dict):
    config = {
        "configurable": {
            "thread_id": str(uuid.uuid4()),
        }
    }
    # NOTE: Configure the right dataset and evaluators
    config["configurable"]["max_structured_output_retries"] = 3
    config["configurable"]["allow_clarification"] = False
    config["configurable"]["max_concurrent_research_units"] = 10
    config["configurable"]["search_api"] = "tavily"     # NOTE: We use Tavily to stay consistent
    config["configurable"]["max_researcher_iterations"] = 3
    config["configurable"]["max_react_tool_calls"] = 10
    config["configurable"]["summarization_model"] = "openai:gpt-4.1-nano"
    config["configurable"]["summarization_model_max_tokens"] = 8192
    config["configurable"]["research_model"] = "openai:gpt-4.1"
    config["configurable"]["research_model_max_tokens"] = 10000
    config["configurable"]["compression_model"] = "openai:gpt-4.1-mini"
    config["configurable"]["compression_model_max_tokens"] = 10000
    config["configurable"]["final_report_model"] = "openai:gpt-4.1"
    config["configurable"]["final_report_model_max_tokens"] = 10000
    # NOTE: We do not use MCP tools to stay consistent
    _run_id, final_state = await run_native_question(
        inputs["messages"], config,
        runs_dir=Path(__file__).resolve().parents[1] / ".runs" / "parallel-evaluation",
    )
    return final_state



async def main():
    from langsmith import Client

    with evaluation_runtime_environment():
        return await Client().aevaluate(
            target,
            data=dataset_name,
            evaluators=[right_parallelism_evaluator],
            experiment_prefix="v1 #",
            max_concurrency=1,
        )

if __name__ == "__main__":
    results = asyncio.run(main())
    print(results)  # noqa: T201


