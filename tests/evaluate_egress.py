"""Evaluate domain classification separately from deterministic authorization tests.

Run without --live to validate the corpus, or provide --predictions JSON results.
Live evaluation explicitly uses the configured model gateway and may incur cost.
"""

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path


def summarize(cases, predictions):
    """Report missing samples and denominators rather than implying perfect safety."""
    by_id = {item["id"]: item for item in predictions}
    matched = [(case, by_id[case["id"]]) for case in cases if case["id"] in by_id]

    def rate(group, verdict):
        subset = [(case, result) for case, result in matched if case["group"] == group]
        return {"count": len(subset), "rate": sum(result["verdict"] == verdict for _, result in subset) / len(subset) if subset else None}

    durations = [result["latency_ms"] for _, result in matched if "latency_ms" in result]
    return {"evaluated": len(matched), "total": len(cases),
            "missing": [case["id"] for case in cases if case["id"] not in by_id],
            "risky_allow": rate("risky", "allow"), "safe_deny": rate("safe", "deny"),
            "manual_rate": sum(result["verdict"] == "ask" for _, result in matched) / len(matched) if matched else None,
            "mean_latency_ms": statistics.mean(durations) if durations else None,
            "model_calls": sum(result.get("model_calls", 0) for _, result in matched),
            "cost_usd": sum(result["cost_usd"] for _, result in matched) if matched and all(result.get("cost_usd") is not None for _, result in matched) else None}


async def evaluate_live(cases, model):
    """Exercise both real classifier stages through the configured model gateway."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from open_deep_research.models.invocation import complete_model
    from open_deep_research.sandbox.egress_classifier import (
        EgressClassifier,
        EgressClassifierLimits,
        EgressDomainVerdict,
        EgressModelReply,
    )

    predictions = []
    for case in cases:
        classifier = EgressClassifier(EgressClassifierLimits())

        async def invoke(call):
            messages = [(SystemMessage if item["role"] == "system" else HumanMessage)(content=item["content"]) for item in call.messages]
            result = await complete_model(messages, {"configurable": {}}, role="egress_classifier",
                stage="researching", model=model, max_output_tokens=call.max_output_tokens,
                temperature=call.temperature, span_name="eval.egress",
                output_schema=EgressDomainVerdict if call.structured_schema else None)
            if isinstance(result, AIMessage):
                return EgressModelReply(status="completed", content=str(result.content))
            return EgressModelReply(status="completed", structured=result.model_dump())

        started = time.monotonic()
        result = await classifier.classify_target(host=case["host"], port=443,
            tool_name="fetch_url", capability="tool.egress", intent=case["intent"], invoker=invoke)
        predictions.append({"id": case["id"], "verdict": result.verdict,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "model_calls": classifier.calls_used, "detail": result.detail,
            "model": model, "cost_usd": None})
    return predictions


def main():
    """Validate samples or emit explicitly sourced evaluation results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "fixtures/egress_cases.json")
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    assert len({case["id"] for case in cases}) == len(cases)
    assert all(case["expected"] in {"allow", "ask", "deny"} and case["reason"] for case in cases)
    if args.live:
        if not args.model:
            parser.error("--live requires --model")
        predictions = asyncio.run(evaluate_live(cases, args.model))
    else:
        predictions = json.loads(args.predictions.read_text(encoding="utf-8")) if args.predictions else []
    result = {"mode": "live" if args.live else "recorded" if args.predictions else "corpus_only",
              "metrics": summarize(cases, predictions), "predictions": predictions}
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)  # noqa: T201 -- Standalone evaluation CLI output.


if __name__ == "__main__":
    main()
