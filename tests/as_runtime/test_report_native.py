"""Native report genres, journal replay and publication worker acceptance."""

from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, StructuredResponse

from open_deep_research.agentscope_runtime.report import (
    NativeReportWriter,
    enqueue_report_publication,
)
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
from open_deep_research.report.profiles import REPORT_PROFILES

BODY = "# Report\n\n## Findings\n\nSupported finding [Source](https://example.com/source).\n\n| Item | Value |\n|---|---|\n| A | Supported |\n"


class Factory:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.run = SimpleNamespace(get=lambda name: {})

    def descriptor(self, role):
        return {"model": "openai:gpt-4.1", "max_output_tokens": 1024}

    def policy_middleware(self, role, candidates=None):
        async def invoke(handler, kwargs, state):
            return await handler(self, **kwargs)

        return SimpleNamespace(policy=SimpleNamespace(invoke=invoke))

    async def generate_structured_output(self, messages, schema):
        self.calls.append(schema.__name__)
        if schema.__name__ == "ReportOutline":
            return StructuredResponse(
                content={
                    "title": "Report",
                    "sections": [
                        {"name": "Findings", "description": "facts"},
                        {"name": "Risks", "description": "limitations"},
                    ],
                }
            )
        raise RuntimeError("fixture review unavailable")

    async def complete_with_recovery(self, role, messages, **kwargs):
        self.calls.append(role)
        if self.fail:
            raise RuntimeError("writer failure")
        assert messages[0].role == "system"
        return ChatResponse(content=[TextBlock(text=BODY)], is_last=True)


def state():
    return ResearchSnapshot(
        run_id="native-report",
        config_fingerprint="fixed",
        messages=[UserMsg("user", "Research the finding")],
        research_brief="Research the finding",
        findings=[
            {
                "research_topic": "finding",
                "compressed_research": "Supported finding",
                "evidence_registry": [],
            }
        ],
    )


def config(tmp_path, **kwargs):
    return {
        "configurable": {
            "web_pipeline_mode": "legacy",
            "quality_evaluation_enabled": False,
            "runs_dir": str(tmp_path),
            "report_review_enabled": False,
            **kwargs,
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("genre", REPORT_PROFILES)
async def test_all_native_genres_preserve_product_contract(tmp_path, genre):
    factory = Factory()
    snapshot = state()
    writer = NativeReportWriter(ResearchModels(factory))
    report = await writer(snapshot, config(tmp_path, report_type=genre))
    assert "Supported finding" in report
    assert snapshot.report_product["canonical_report"]["run_id"] == snapshot.run_id
    assert factory.calls
    snapshot.model_dump(mode="json")


@pytest.mark.asyncio
async def test_native_writer_error_never_becomes_body(tmp_path):
    factory = Factory()
    factory.fail = True
    snapshot = state()
    with pytest.raises(RuntimeError, match="writer failure"):
        await NativeReportWriter(ResearchModels(factory))(snapshot, config(tmp_path))
    assert snapshot.final_report == "" and snapshot.report_product == {}


@pytest.mark.asyncio
async def test_native_review_outage_fail_closed(tmp_path):
    with pytest.raises(RuntimeError):
        await NativeReportWriter(ResearchModels(Factory()))(
            state(),
            config(tmp_path, report_review_enabled=True, report_review_fail_open=False),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", [False, True])
async def test_native_review_pass_and_explicit_fail_open(tmp_path, outage):
    snapshot = state()
    snapshot.findings[0]["evidence_registry"] = [
        {
            "evidence_id": "ev1",
            "claim": "Supported finding",
            "supporting_excerpt": "Supported finding",
            "source_url": "https://example.com/source",
            "security_status": "accepted",
        }
    ]
    factory = Factory()

    async def review(messages, schema):
        factory.calls.append(schema.__name__)
        if outage:
            raise RuntimeError("fixture transport unavailable")
        return StructuredResponse(
            content={
                "decision": "pass",
                "dimensions": {
                    key: 1.0
                    for key in [
                        "coverage",
                        "citation_correctness",
                        "contradictions",
                        "unsupported_claims",
                        "redundancy",
                        "executive_readability",
                    ]
                },
                "citation_audit": [
                    {
                        "claim": "Supported finding",
                        "citation_target": "https://example.com/source",
                        "supported": True,
                        "evidence_ids": ["ev1"],
                    }
                ],
            }
        )

    factory.generate_structured_output = review
    report = await NativeReportWriter(ResearchModels(factory))(
        snapshot,
        config(tmp_path, report_review_enabled=True, report_review_fail_open=True),
    )
    assert "Supported finding" in report
    assert "ReportReview" in factory.calls
    assert snapshot.report_product["report_review"]["decision"] == "pass"
    if outage:
        assert snapshot.report_product["report_review"]["skipped"]


@pytest.mark.asyncio
async def test_publication_requires_completed_run_and_deduplicates(tmp_path):
    from open_deep_research.agentscope_runtime.recovery import RecoverySession
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    from open_deep_research.report.models import PublisherTheme
    from open_deep_research.report.publication_store import (
        PublicationJobStore,
        PublisherSettings,
    )
    from open_deep_research.report.publisher_worker import PublisherWorker

    store = RecoveryStore(
        "sqlite+aiosqlite:///" + (tmp_path / "recovery.db").as_posix()
    )
    await store.create_tables()
    snapshot = state()
    await store.create_run("owner", snapshot)
    session = await RecoverySession.open(store, snapshot.run_id, "owner")
    theme = PublisherTheme(locale="en-US")
    try:
        with pytest.raises(ValueError, match="not completed"):
            await enqueue_report_publication(
                session,
                snapshot,
                publication_format="markdown",
                theme=theme,
                runs_dir=tmp_path,
            )
        snapshot.final_report = await NativeReportWriter(ResearchModels(Factory()))(
            snapshot, config(tmp_path)
        )
        snapshot.status = "completed"
        await store.save(session.lease, snapshot)
        first = await enqueue_report_publication(
            session,
            snapshot,
            publication_format="markdown",
            theme=theme,
            runs_dir=tmp_path,
        )
        again = await enqueue_report_publication(
            session,
            snapshot,
            publication_format="markdown",
            theme=theme,
            runs_dir=tmp_path,
        )
        assert first["publication_id"] == again["publication_id"]
        worker = PublisherWorker(PublisherSettings(runs_dir=tmp_path))
        assert await worker.run_once() == 1
        jobs = PublicationJobStore(snapshot.run_id, runs_dir=tmp_path)
        assert jobs.get(first["publication_id"]).status == "completed"
        assert await worker.run_once() == 0
    finally:
        await session.close()
        await store.aclose()


@pytest.mark.asyncio
async def test_model_commit_window_replays_without_second_writer_call(tmp_path):
    from open_deep_research.agentscope_runtime.recovery import RecoverySession
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore

    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "journal.db").as_posix())
    await store.create_tables()
    snapshot = state()
    await store.create_run("owner", snapshot)
    first = await RecoverySession.open(store, snapshot.run_id, "owner")
    factory = Factory()

    async def crash(point):
        if point == "operation_committed":
            raise KeyboardInterrupt("simulated process exit")

    first.failpoint = crash
    try:
        with (
            first.scope("final_report_generation", 0),
            pytest.raises(KeyboardInterrupt),
        ):
            await NativeReportWriter(ResearchModels(factory, recovery=first))(
                snapshot, config(tmp_path)
            )
    finally:
        await first.close()
    resumed = await RecoverySession.open(store, snapshot.run_id, "owner")
    try:
        with resumed.scope("final_report_generation", 0):
            report = await NativeReportWriter(
                ResearchModels(factory, recovery=resumed)
            )(state(), config(tmp_path))
        assert "Supported finding" in report and factory.calls == ["final_report"]
    finally:
        await resumed.close()
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fmt",
    [
        "markdown",
        "json",
        "pdf",
        "docx",
        "pptx",
        "one_pager",
        "slides",
        "structured_json",
    ],
)
async def test_actual_publishers_all_formats(tmp_path, fmt):
    from open_deep_research.report.canonical import canonicalize_report
    from open_deep_research.report.models import PublisherTheme
    from open_deep_research.report.publishers import (
        render_publication,
        validate_rendered_artifact,
    )

    canonical = canonicalize_report(
        BODY,
        run_id="formats",
        report_type="default",
        locale="en-US",
        sources=[{"title": "Source", "url": "https://example.com/source"}],
    )
    artifact = render_publication(canonical, fmt, PublisherTheme(locale="en-US"))
    validate_rendered_artifact(canonical, fmt, artifact)
    (tmp_path / (fmt + "." + artifact.extension)).write_bytes(artifact.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["before_render", "file_committed", "job_completed"])
async def test_killed_publication_worker_recovers_one_job_and_one_terminal_event(
    tmp_path, window
):
    import asyncio
    import hashlib
    import os
    import sys
    from pathlib import Path

    from open_deep_research.events.publications import PublicationEventStore
    from open_deep_research.report.canonical import canonicalize_report
    from open_deep_research.report.models import PublisherTheme
    from open_deep_research.report.publication_store import (
        PublicationJobStore,
        PublisherSettings,
    )
    from open_deep_research.report.publisher_worker import PublisherWorker
    from open_deep_research.run_context import RunContextStore

    run_id = "worker-kill"
    canonical = canonicalize_report(
        BODY,
        run_id=run_id,
        report_type="default",
        locale="en-US",
        sources=[{"title": "Source", "url": "https://example.com/source"}],
    )
    RunContextStore(run_id, runs_dir=tmp_path).write_text_atomic(
        "final_report.md", BODY
    )
    jobs = PublicationJobStore(run_id, runs_dir=tmp_path)
    jobs.persist_canonical_report(canonical.model_dump(mode="json"))
    job, _ = jobs.enqueue(
        report_sha256=hashlib.sha256(BODY.encode()).hexdigest(),
        publication_format="markdown",
        theme=PublisherTheme(),
        max_attempts=3,
    )
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "tests/as_runtime/report_worker_process.py",
        str(tmp_path),
        window,
        env=dict(os.environ, PYTHONPATH=str(Path("src").resolve())),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        for _ in range(200):
            if (tmp_path / "ready").exists() or child.returncode is not None:
                break
            await asyncio.sleep(0.1)
        assert (tmp_path / "ready").exists(), "Worker did not reach the crash window"
    finally:
        if child.returncode is None:
            child.kill()
        await child.communicate()
    await asyncio.sleep(1.3)
    worker = PublisherWorker(PublisherSettings(runs_dir=tmp_path, lease_seconds=1))
    await worker.run_once()
    await worker.run_once()
    assert jobs.get(job.publication_id).status == "completed"
    assert len(jobs.list()) == 1
    events = PublicationEventStore(run_id, runs_dir=tmp_path).read()
    assert sum(e.type == "publication.completed" for e in events) == 1
