"""Native model and durable publication ports for the existing report product."""

from contextlib import nullcontext

from agentscope.message import SystemMsg, TextBlock, UserMsg

from open_deep_research.report.runtime import ReportMessage, native_report


class NativeReportWriter:
    """Preserve domain report rules while native models own calls and retries."""

    resumable = True

    def __init__(self, models):
        self.models = models

    async def outline(self, snapshot, config):
        """Budget approval outlines using the same evidence authority as writing."""
        token = native_report.set(_ReportRun(self.models, snapshot))
        try:
            return await self._outline(snapshot, config)
        finally:
            native_report.reset(token)

    async def _outline(self, snapshot, config):
        from open_deep_research.configuration import Configuration
        from open_deep_research.report.assembly import ReportContext
        from open_deep_research.report.profiles import get_profile

        cfg = Configuration.from_runnable_config(config)
        context = ReportContext.from_state(
            {
                "research_brief": snapshot.research_brief,
                "notes": [item.get("compressed_research", "") for item in snapshot.findings],
                "evidence_registry": [record for item in snapshot.findings for record in item.get("evidence_registry", [])],
                "coverage_contract": snapshot.coverage_contract,
                "coverage_ledger": snapshot.coverage_ledger,
            },
            config,
            get_profile(cfg.report_type),
        )
        messages = context.stage_messages(
            "基于已准入证据编写待用户审批的报告大纲，保留需求归属、缺口和用户修订反馈。",
            {"brief": snapshot.research_brief, "feedback": snapshot.feedback,
             "completion_outcome": snapshot.completion_outcome},
        )
        response = await native_report.get().invoke(
            "final_report", messages, cfg, span_name="approval_outline"
        )
        return response.content

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
                "supervisor_messages": snapshot.agent_states.get("supervisor", {}).get("context", []),
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
            if snapshot.completion_outcome.get("action") == "complete_partial" and (
                snapshot.completion_outcome.get("reason") in {"report_budget_reserved", "report_time_reserved"}
                or not state["evidence_registry"]
            ):
                from open_deep_research.configuration import Configuration
                from open_deep_research.report.orchestrator import (
                    _canonical_report_payload,
                )
                from open_deep_research.report.profiles import get_profile
                from open_deep_research.report.recovery import (
                    build_evidence_recovery_report,
                )


                reason = snapshot.completion_outcome.get("reason", "insufficient_evidence")
                markdown = build_evidence_recovery_report(state["evidence_registry"],
                    gaps=snapshot.completion_outcome.get("gaps", []), rejection_reasons=[reason], artifact_refs=[])
                update = {"final_report": markdown, "completion_decision": snapshot.completion_outcome,
                    "quality_gate": {"status": "partial", "reason_codes": [reason]},
                    "report_review": {"status": "skipped", "skipped": True, "degraded": True,
                        "summary": "仅交付确定性证据恢复结果，未执行模型报告复核：" + reason,
                        "decision": "fail"} if Configuration.from_runnable_config(config).report_review_enabled else None,
                    "canonical_report": _canonical_report_payload(markdown, state, bound_config,
                        get_profile(Configuration.from_runnable_config(config).report_type), [])}
            else:
                update = await build_report(state, bound_config)
            final_decision = update.get("completion_decision") or snapshot.completion_outcome
            update["completion_status"] = "partial" if final_decision.get("action") == "complete_partial" else "success"
            update["stop_reason"] = final_decision.get("reason")
            update["uncovered_requirements"] = [row["requirement_id"] for row in snapshot.coverage_contract.get("requirements", [])
                if row.get("kind", "factual") == "factual" and snapshot.coverage_ledger.get(row["requirement_id"], {}).get("status") != "supported"]
            update["research_gaps"] = final_decision.get("gaps", [])
            snapshot.report_product = {
                key: value
                for key, value in update.items()
                if key not in {"messages", "notes", "completed_task_outputs"}
            }
            from open_deep_research.quality.planning import unique_evidence
            from open_deep_research.report.references import parse_sources_from_text
            cited = {source.url.rstrip("/") for source in parse_sources_from_text(update.get("final_report", ""))}
            contribution = {}
            for record in unique_evidence(state["evidence_registry"]):
                for provider in record.get("discovery_providers", []):
                    row = contribution.setdefault(provider, {"evidence_ids": [], "source_urls": [], "cited_source_urls": []})
                    row["evidence_ids"].append(record["evidence_id"])
                    url = record["source_url"].rstrip("/")
                    if url not in row["source_urls"]:
                        row["source_urls"].append(url)
                    if url in cited and url not in row["cited_source_urls"]:
                        row["cited_source_urls"].append(url)
            snapshot.report_product["provider_contribution"] = contribution
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

    async def archive_review(self, draft, review, *, attempt, revision_count):
        """Persist exactly the submitted draft; failure never erases its audit."""
        import hashlib
        import json

        from open_deep_research.agentscope_runtime.storage import runtime_data_dir
        from open_deep_research.quality.planning import unique_evidence

        recovery = self.models.recovery
        if recovery is None:
            return
        sha = hashlib.sha256(draft.markdown.encode("utf8", errors="replace")).hexdigest()
        directory = runtime_data_dir() / self.snapshot.run_id / "report-reviews"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (sha + ".md")).write_bytes(draft.markdown.encode("utf8", errors="replace"))
        records = unique_evidence([r for task in self.snapshot.findings for r in task.get("evidence_registry", [])])
        prior = self.snapshot.application.get("report_review_artifacts", {}).get(sha, {})
        manifest = {"sha256": sha, "attempt": attempt, "revision_count": revision_count,
            "coverage_contract": self.snapshot.coverage_contract,
            "evidence_ids": [r["evidence_id"] for r in records],
            "evidence_selections": self.snapshot.application.get("report_evidence_selections", {}),
            "review": review.model_dump(mode="json") if review else prior.get("review"),
            "scope": "review_draft", "delivered": False}
        self.review_submission = (draft, attempt, revision_count)
        (directory / (sha + ".json")).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf8")
        self.snapshot.application.setdefault("report_review_artifacts", {})[sha] = manifest
        await recovery.save(self.snapshot)

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
            import json
            ids = [r.get("evidence_id") for m in fitted if m.name == "report_evidence"
                   for r in json.loads(str(m.content)).get("records", [])]
            self.snapshot.application.setdefault("report_evidence_selections", {})[task_id] = ids
            return [
                (SystemMsg if m.type == "system" else UserMsg)(
                    m.name or m.type, m.content
                )
                for m in fitted
            ]

        initial = fit(0)

        def fit_candidate(current_model, current, output_tokens):
            fitted, selected = fit_writing_messages(
                [ReportMessage(message.get_text_content() or "", name=message.name,
                               type="system" if message.role == "system" else "human")
                 for message in current],
                descriptor["model"], cfg, output_tokens=output_tokens,
                context_window=getattr(current_model, "context_size", None),
            )
            self.score(span_name + ".selected_evidence_count", selected)
            return [message.model_copy(update={"content": [TextBlock(text=projected.content)]})
                    for message, projected in zip(current, fitted, strict=True)]

        async def call():
            for attempt in range(3):
                native = initial if attempt == 0 else fit(attempt)
                if span_name == "lead.report_review" and hasattr(self, "review_submission"):
                    draft, review_attempt, revision_count = self.review_submission
                    await self.archive_review(draft, None, attempt=review_attempt, revision_count=revision_count)
                try:
                    if schema is not None:
                        policy = factory.policy_middleware(role, candidates=candidates)

                        async def invoke(current_model, messages, **kwargs):
                            return await current_model.generate_structured_output(
                                fit_candidate(current_model, messages, descriptor["max_output_tokens"]), schema
                            )

                        return await policy.policy.invoke(
                            invoke, {"messages": native}, {}
                        )
                    return await factory.complete_with_recovery(
                        role, native, state={}, candidates=candidates,
                        prepare_messages=fit_candidate,
                    )
                except Exception as exc:
                    if attempt == 2 or not is_token_limit_exceeded(
                        exc, descriptor["model"]
                    ):
                        raise
                    self.record_retry(
                        attempt=attempt + 1, error_type="context_length_exceeded"
                    )

        from open_deep_research.agentscope_runtime.runtime_limits import attributed

        with (recovery.task(task_id) if recovery else nullcontext()), attributed(purpose=span_name):
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
