"""Native model and durable publication ports for the existing report product."""

from contextlib import nullcontext

from agentscope.message import SystemMsg, TextBlock, UserMsg

from open_deep_research.report.runtime import ReportMessage, native_report


class NativeReportWriter:
    """Preserve domain report rules while native models own calls and retries."""

    resumable = True

    def __init__(self, models):
        self.models = models

    async def __call__(self, snapshot, config):
        from open_deep_research.report.orchestrator import build_report

        port = _ReportRun(self.models, snapshot)
        token = native_report.set(port)
        try:
            state = {
                "messages": [
                    ReportMessage(
                        m.get_text_content() or "",
                        type="human" if m.role == "user" else "ai",
                    )
                    for m in snapshot.messages
                ],
                "research_brief": snapshot.research_brief,
                "notes": [f.get("compressed_research", "") for f in snapshot.findings],
                "completed_task_outputs": snapshot.findings,
                "evidence_registry": [
                    e for f in snapshot.findings for e in f.get("evidence_registry", [])
                ],
                "coverage_contract": snapshot.coverage_contract,
                "coverage_ledger": snapshot.coverage_ledger,
                "research_risk_profile": snapshot.research_risk_profile,
                "completion_outcome": snapshot.completion_outcome,
                "completion_decision": snapshot.completion_outcome,
                "report_outline": snapshot.outline,
            }
            bound_config = {
                **config,
                "metadata": {**config.get("metadata", {}), "run_id": snapshot.run_id},
            }
            update = await build_report(state, bound_config)
            snapshot.report_product = {
                key: value
                for key, value in update.items()
                if key not in {"messages", "notes", "completed_task_outputs"}
            }
            return update["final_report"]
        finally:
            native_report.reset(token)


class _ReportRun:
    def __init__(self, models, snapshot):
        self.models, self.snapshot = models, snapshot
        self.counts = {}

    def active_span(self):
        return self

    def score(self, name, value, *args, **kwargs):
        self.snapshot.agent_states.setdefault("report_metrics", {})[name] = value

    def record_retry(self, **kwargs):
        self.snapshot.agent_states.setdefault("report_retries", []).append(kwargs)

    async def invoke(self, role, messages, cfg, *, span_name, schema=None):
        from open_deep_research.models.errors import is_token_limit_exceeded
        from open_deep_research.report.writing import fit_writing_messages

        index = self.counts.get(span_name, 0)
        self.counts[span_name] = index + 1
        task_id = f"report:{span_name}:{index}"
        factory = self.models.factory
        descriptor = factory.descriptor(role)
        recovery = self.models.recovery
        candidates = (
            [self.models.model_for(role, task_id)] if self.models.model_for else None
        )

        def fit(attempt):
            fitted, selected = fit_writing_messages(
                messages,
                descriptor["model"],
                cfg,
                output_tokens=descriptor["max_output_tokens"],
                fraction=0.75**attempt,
            )
            self.score(span_name + ".selected_evidence_count", selected)
            return [
                (SystemMsg if m.type == "system" else UserMsg)(
                    m.name or m.type, m.content
                )
                for m in fitted
            ]

        initial = fit(0)

        async def call():
            for attempt in range(3):
                native = initial if attempt == 0 else fit(attempt)
                try:
                    if schema is not None:
                        policy = factory.policy_middleware(role, candidates=candidates)

                        async def invoke(model, messages, **kwargs):
                            return await model.generate_structured_output(
                                messages, schema
                            )

                        return await policy.policy.invoke(
                            invoke, {"messages": native}, {}
                        )
                    return await factory.complete_with_recovery(
                        role, native, state={}, candidates=candidates
                    )
                except Exception as exc:
                    if attempt == 2 or not is_token_limit_exceeded(
                        exc, descriptor["model"]
                    ):
                        raise
                    self.record_retry(
                        attempt=attempt + 1, error_type="context_length_exceeded"
                    )

        with recovery.task(task_id) if recovery else nullcontext():
            response = (
                await recovery.model(
                    role,
                    initial,
                    call,
                    schema=schema,
                    max_tokens=descriptor["max_output_tokens"],
                    pricing=self.models.pricing(role),
                )
                if recovery
                else await call()
            )
        if schema is not None:
            return schema.model_validate(response.content)
        content = "".join(
            b.text for b in response.content if isinstance(b, TextBlock)
        ).strip()
        if not content:
            raise ValueError("report writer returned empty text")
        return ReportMessage(content, type="ai")


async def enqueue_report_publication(
    recovery, snapshot, *, publication_format, theme, runs_dir, max_attempts=3
):
    """Publish only a completed authorized run; replay uses the existing job key."""
    import hashlib

    from open_deep_research.events.publications import (
        PublicationEventStore,
        publication_event_payload,
    )
    from open_deep_research.report.publication_store import PublicationJobStore
    from open_deep_research.run_context import RunContextStore

    persisted, _ = await recovery.store.load(
        recovery.lease.run_id, recovery.lease.user_id
    )
    if persisted.status != "completed" or not persisted.final_report:
        raise ValueError("report run is not completed")
    if (
        snapshot.run_id != recovery.lease.run_id
        or snapshot.final_report != persisted.final_report
    ):
        raise ValueError("report does not match authorized completed run")
    async with recovery.store.transaction(recovery.lease):
        store = RunContextStore(snapshot.run_id, runs_dir=runs_dir)
        store.write_text_atomic("final_report.md", snapshot.final_report)
        jobs = PublicationJobStore(snapshot.run_id, runs_dir=runs_dir)
        canonical = persisted.report_product.get("canonical_report")
        if canonical:
            jobs.persist_canonical_report(canonical)
        job, _ = jobs.enqueue(
            report_sha256=hashlib.sha256(snapshot.final_report.encode()).hexdigest(),
            publication_format=publication_format,
            theme=theme,
            max_attempts=max_attempts,
            on_created=lambda created: PublicationEventStore(
                snapshot.run_id, runs_dir=runs_dir
            ).append(
                "publication.queued",
                publication_id=created.publication_id,
                payload=publication_event_payload(created),
                dedupe_key=created.publication_id + ":queued",
            ),
        )
    return job.public_dict()
