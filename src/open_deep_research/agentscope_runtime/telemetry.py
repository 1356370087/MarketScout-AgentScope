"""Content-free AgentScope lifecycle tracing and durable operational metrics.

Native middleware deliberately omits message bodies, tool arguments and exception
text. SQL receipts, not sampled spans, remain the accounting authority.
"""

import base64
import logging
from contextlib import contextmanager
from functools import lru_cache

from agentscope.middleware import MiddlewareBase
from opentelemetry import context, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Status, StatusCode

from open_deep_research.configuration import Configuration

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def provider():
    """Configure trusted process-level destinations, never user-supplied secrets."""
    cfg = Configuration.from_runnable_config(None)
    if not cfg.observability_enabled or not (cfg.otel_enabled or cfg.langfuse_enabled):
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    value = TracerProvider(
        resource=Resource.create(
            {
                "service.name": cfg.otel_service_name,
                "service.namespace": "insightforge",
                "deployment.environment.name": cfg.langfuse_environment,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(cfg.langfuse_sample_rate)),
    )
    if cfg.otel_enabled:
        endpoint = (cfg.otel_exporter_otlp_endpoint or "http://localhost:4318").rstrip(
            "/"
        )
        if not endpoint.endswith("/v1/traces"):
            endpoint += "/v1/traces"
        value.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
    if cfg.langfuse_enabled:
        if not cfg.langfuse_public_key or not cfg.langfuse_secret_key:
            logger.warning("Native Langfuse export disabled: credentials unavailable")
        else:
            token = base64.b64encode(
                f"{cfg.langfuse_public_key}:{cfg.langfuse_secret_key}".encode()
            ).decode()
            value.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=cfg.langfuse_base_url.rstrip("/")
                        + "/api/public/otel/v1/traces",
                        headers={
                            "Authorization": "Basic " + token,
                            "x-langfuse-ingestion-version": "4",
                        },
                    )
                )
            )
    return value


def start_span(name, *, run_id, task_id, role="unknown"):
    sink = provider()
    if sink is None:
        return trace.INVALID_SPAN
    return sink.get_tracer("insightforge.agentscope").start_span(
        name,
        attributes={
            "insightforge.run_id": run_id,
            "insightforge.task_id": task_id,
            "gen_ai.agent.name": role,
            "session.id": run_id,
            "langfuse.observation.type": "agent"
            if name == "agent.reply"
            else "generation"
            if name == "native.model"
            else "span",
        },
    )


@contextmanager
def operation_span(kind, session, role="unknown"):
    span = start_span(
        "native." + kind,
        run_id=session.lease.run_id,
        task_id=session.task_id.get(),
        role=role or "unknown",
    )
    span.set_attribute("insightforge.stage", session.stage.get().rsplit(":", 1)[0])
    token = context.attach(trace.set_span_in_context(span))
    try:
        yield span
    except BaseException as exc:
        span.set_attribute("error.type", type(exc).__name__)
        span.set_status(Status(StatusCode.ERROR))
        raise
    finally:
        context.detach(token)
        span.end()


class NativeTelemetryMiddleware(MiddlewareBase):
    """Use native reply hooks while avoiding context leaks across generator yields."""

    def __init__(self, recovery, role):
        self.recovery, self.role = recovery, role

    async def on_reply(self, agent, input_kwargs, next_handler):
        span = start_span(
            "agent.reply",
            run_id=self.recovery.lease.run_id,
            task_id=self.recovery.task_id.get(),
            role=self.role,
        )
        generator = next_handler(**input_kwargs)
        try:
            while True:
                token = context.attach(trace.set_span_in_context(span))
                try:
                    item = await anext(generator)
                except StopAsyncIteration:
                    break
                finally:
                    context.detach(token)
                yield item
        except BaseException as exc:
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            await generator.aclose()
            span.end()


def flush():
    """Flush the native process exporter on normal shutdown."""
    if provider.cache_info().currsize:
        sink = provider()
        if sink is not None:
            sink.force_flush(timeout_millis=5000)


async def prometheus_snapshot(store):
    """Aggregate all API/worker receipts without per-run metric labels or replay counts."""
    from prometheus_client import CollectorRegistry, Gauge, generate_latest
    from sqlalchemy import func, select, text

    registry = CollectorRegistry()
    count = Gauge(
        "insightforge_native_operations",
        "Durable operation count by kind and state.",
        ["kind", "state"],
        registry=registry,
    )
    tokens = Gauge(
        "insightforge_native_tokens",
        "Settled native tokens including conservative estimates.",
        ["direction"],
        registry=registry,
    )
    runs = Gauge(
        "insightforge_native_runs",
        "Persisted native run count by status.",
        ["status"],
        registry=registry,
    )
    tools = Gauge(
        "insightforge_native_tools",
        "Settled physical tool outcomes; unknown is not success.",
        ["status"],
        registry=registry,
    )
    async with store.engine.connect() as conn:
        for kind, state, total in await conn.execute(
            select(store.ops.c.kind, store.ops.c.state, func.count()).group_by(
                store.ops.c.kind, store.ops.c.state
            )
        ):
            count.labels(kind, state).set(total)
        for direction in ("input_tokens", "output_tokens"):
            total = await conn.scalar(
                select(func.sum(store.ops.c.actual[direction].as_integer())).where(
                    store.ops.c.kind.in_(["gateway:model", "model_attempt"]),
                    store.ops.c.state == "committed",
                )
            )
            tokens.labels(direction).set(total or 0)
        status = store.runs.c.snapshot["status"].as_string()
        for state, total in await conn.execute(
            select(status, func.count()).group_by(status)
        ):
            runs.labels(state).set(total)
        from open_deep_research.agentscope_runtime.usage_projection import tool_success

        rows = (
            (
                await conn.execute(
                    select(
                        store.ops.c.kind, store.ops.c.state, store.ops.c.result
                    ).where(
                        store.ops.c.kind.in_(["tool", "gateway:tool"]),
                        store.ops.c.reservation["tool_calls"].as_integer() > 0,
                    )
                )
            )
            .mappings()
            .all()
        )
        counts = {"success": 0, "error": 0, "unknown": 0}
        for row in rows:
            ok = tool_success(row)
            counts["unknown" if ok is None else "success" if ok else "error"] += 1
        for status, total in counts.items():
            tools.labels(status).set(total)
        if store.engine.dialect.name == "postgresql" and await conn.scalar(text("SELECT to_regclass('research_team_plans')")):
            queries = {
                "claim_conflicts": ("CAS task claim conflicts.", "SELECT count(*) FROM research_coordination_transactions WHERE event->>'type'='task_claim' AND result->>'claimed'='false'"),
                "message_backlog": ("Unpublished durable team messages.", "SELECT count(*) FROM research_coordination_outbox WHERE published_at IS NULL"),
                "member_recoveries": ("Persisted member restart count.", "SELECT coalesce(sum(greatest(execution_epoch-1,0)),0) FROM research_team_members"),
                "plan_review_seconds": ("Mean persisted plan review duration in seconds.", "SELECT coalesce(avg(extract(epoch FROM reviewed_at-created_at)),0) FROM research_team_plans WHERE reviewed_at IS NOT NULL"),
            }
            for name, (description, query) in queries.items():
                Gauge("insightforge_team_" + name, description, registry=registry).set(float(await conn.scalar(text(query))))
    return generate_latest(registry)
