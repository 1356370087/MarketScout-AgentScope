"""Authenticated trace queries and the server-rendered observability view."""

from __future__ import annotations

import html
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from open_deep_research.configuration import Configuration
from open_deep_research.observability import SQLiteTraceStore
from security.rbac import Principal, require_permissions
from security.rbac.permissions import RESEARCH_OBSERVABILITY_READ_OWN

router = APIRouter(tags=["observability"])


def _observability_store() -> SQLiteTraceStore:
    configurable = Configuration.from_runnable_config(None)
    return SQLiteTraceStore(configurable.trace_store_path)


def _span_tree_rows(spans: list[dict[str, Any]]) -> str:
    children: dict[str | None, list[dict[str, Any]]] = {}
    for span in spans:
        children.setdefault(span.get("parent_span_id"), []).append(span)

    rows: list[str] = []

    def visit(span: dict[str, Any], depth: int) -> None:
        status = html.escape(str(span.get("status") or ""))
        name = html.escape(str(span.get("name") or ""))
        kind = html.escape(str(span.get("kind") or ""))
        duration = span.get("duration_ms") or 0
        tokens = span.get("total_tokens") or 0
        retry_count = span.get("retry_count") or 0
        error_type = html.escape(str(span.get("error_type") or ""))
        error = html.escape(str(span.get("error") or ""))
        indent = "&nbsp;" * depth * 4
        cls = "error" if status == "error" else "ok"
        rows.append(
            f"<tr class='{cls}'><td>{indent}{name}</td><td>{kind}</td>"
            f"<td>{status}</td><td>{duration}</td><td>{tokens}</td>"
            f"<td>{retry_count}</td><td>{error_type}</td><td>{error}</td></tr>"
        )
        for child in children.get(span.get("span_id"), []):
            visit(child, depth + 1)

    roots = children.get(None, [])
    for root in roots:
        visit(root, 0)
    for span in spans:
        if span.get("parent_span_id") and span.get("parent_span_id") not in {
            s.get("span_id") for s in spans
        }:
            visit(span, 0)
    return "".join(rows)


@router.get("/observability/runs")
async def list_observed_runs(
    limit: int = 100,
    user: Principal = Depends(
        require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)
    ),
) -> dict[str, Any]:
    """Return persisted observed run summaries."""
    store = _observability_store()
    return {"runs": store.list_runs(limit=limit, user_id=user.user_id)}


@router.get("/observability/runs/{run_id}")
async def get_observed_run(
    run_id: str,
    user: Principal = Depends(
        require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)
    ),
) -> dict[str, Any]:
    """Return one persisted observed run summary."""
    store = _observability_store()
    run = store.get_run(run_id, user_id=user.user_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Observed run not found")
    return {"run": run}


@router.get("/observability/runs/{run_id}/spans")
async def get_observed_run_spans(
    run_id: str,
    user: Principal = Depends(
        require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)
    ),
) -> dict[str, Any]:
    """Return ordered spans for a persisted observed run."""
    store = _observability_store()
    if store.get_run(run_id, user_id=user.user_id) is None:
        raise HTTPException(status_code=404, detail="Observed run not found")
    return {"run_id": run_id, "spans": store.list_spans(run_id)}


@router.get("/observability/runs/{run_id}/usage")
async def get_observed_run_usage(
    run_id: str,
    user: Principal = Depends(
        require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)
    ),
) -> dict[str, Any]:
    """Return token usage aggregate for a persisted observed run."""
    store = _observability_store()
    if store.get_run(run_id, user_id=user.user_id) is None:
        raise HTTPException(status_code=404, detail="Observed run not found")
    return {"run_id": run_id, "usage": store.get_usage(run_id)}


@router.get("/observability/runs/{run_id}/metrics")
async def get_observed_run_metrics(
    run_id: str,
    user: Principal = Depends(
        require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)
    ),
) -> dict[str, Any]:
    """Return token usage, retry counts, and 429 rate for a persisted observed run."""
    store = _observability_store()
    if store.get_run(run_id, user_id=user.user_id) is None:
        raise HTTPException(status_code=404, detail="Observed run not found")
    return {"run_id": run_id, "metrics": store.get_metrics(run_id)}


@router.get("/observability/ui", response_class=HTMLResponse)
async def observability_ui(
    run_id: str | None = None,
    user: Principal = Depends(
        require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)
    ),
) -> HTMLResponse:
    """Render a small server-side observability page."""
    store = _observability_store()
    identity = user.user_id
    runs = store.list_runs(limit=50, user_id=identity)
    selected = run_id or (runs[0]["run_id"] if runs else None)
    selected_run = store.get_run(selected, user_id=identity) if selected else None
    if selected and selected_run is None:
        raise HTTPException(status_code=404, detail="Observed run not found")
    spans = store.list_spans(selected) if selected else []
    run_links = "".join(
        "<li><a href='/observability/ui?run_id="
        + html.escape(str(run["run_id"]))
        + "'>"
        + html.escape(str(run["run_id"]))
        + "</a> "
        + html.escape(str(run.get("status") or ""))
        + " "
        + str(run.get("total_tokens") or 0)
        + " tokens</li>"
        for run in runs
    )
    usage = (
        store.get_usage(selected)
        if selected
        else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )
    metrics = (
        store.get_metrics(selected)
        if selected
        else {
            "retry_count": 0,
            "rate_limited_count": 0,
            "rate_429": 0.0,
            "total_llm_tool_calls": 0,
            "cache_hit_rate": 0.0,
            "tool_success_rate": 0.0,
        }
    )
    run_title = html.escape(str(selected or "No observed runs"))
    run_status = html.escape(str((selected_run or {}).get("status") or ""))
    rows = _span_tree_rows(spans)
    rate_pct = f"{(metrics.get('rate_429') or 0) * 100:.1f}%"
    cache_hit_pct = f"{(metrics.get('cache_hit_rate') or 0) * 100:.1f}%"
    cache_input_pct = f"{(metrics.get('cache_input_ratio') or 0) * 100:.1f}%"
    tool_success_pct = f"{(metrics.get('tool_success_rate') or 0) * 100:.1f}%"
    body = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset='utf-8'>
      <title>Open Deep Research Observability</title>
      <style>
        body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1f2937; }}
        main {{ display: grid; grid-template-columns: 320px 1fr; gap: 24px; }}
        a {{ color: #0f766e; text-decoration: none; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
        th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; vertical-align: top; }}
        th {{ background: #f8fafc; }}
        .metric {{ display: inline-block; margin-right: 16px; padding: 8px 10px; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 6px; }}
        .error td {{ background: #fef2f2; }}
        .ok td {{ background: #ffffff; }}
        aside {{ border-right: 1px solid #e5e7eb; padding-right: 16px; }}
        ul {{ padding-left: 18px; }}
      </style>
    </head>
    <body>
      <h1>Open Deep Research Observability</h1>
      <main>
        <aside>
          <h2>Runs</h2>
          <ul>{run_links}</ul>
        </aside>
        <section>
          <h2>{run_title}</h2>
          <div class='metric'>status: {run_status}</div>
          <div class='metric'>input: {usage["input_tokens"]}</div>
          <div class='metric'>output: {usage["output_tokens"]}</div>
          <div class='metric'>total: {usage["total_tokens"]}</div>
          <div class='metric'>cached input: {usage.get("cached_input_tokens", 0)}</div>
          <div class='metric'>reasoning: {usage.get("reasoning_tokens", 0)}</div>
          <div class='metric'>estimated cost: ${usage.get("estimated_cost_usd", 0):.6f}</div>
          <div class='metric'>attempts: {metrics.get("attempt_count", 0)}</div>
          <div class='metric'>retries: {metrics.get("retry_count", 0)}</div>
          <div class='metric'>429 call rate: {rate_pct} ({metrics.get("rate_limited_count", 0)}/{metrics.get("total_llm_tool_calls", 0)} calls; {metrics.get("rate_limit_events", 0)} events)</div>
          <div class='metric'>cache hit: {cache_hit_pct} ({metrics.get("cache_hit_count", 0)}/{metrics.get("cache_eligible_count", 0)} calls)</div>
          <div class='metric'>cached input ratio: {cache_input_pct}</div>
          <div class='metric'>output throughput: {metrics.get("llm_output_tokens_per_second", 0):.1f} token/s</div>
          <div class='metric'>tool success: {tool_success_pct} ({metrics.get("tool_success_count", 0)}/{metrics.get("tool_call_count", 0)})</div>
          <div class='metric'>empty tool results: {metrics.get("empty_tool_result_count", 0)}</div>
          <div class='metric'>zero-source searches: {metrics.get("zero_source_search_count", 0)}</div>
          <table>
            <thead><tr><th>Span</th><th>Kind</th><th>Status</th><th>Duration ms</th><th>Tokens</th><th>Retries</th><th>Error type</th><th>Error</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </section>
      </main>
    </body>
    </html>
    """
    return HTMLResponse(body)
