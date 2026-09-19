"""Native public findings use SQL replay and never fall back to raw material."""

from types import SimpleNamespace

import pytest
from agentscope.model import StructuredResponse
from test_recovery import create, store  # noqa: F401
from test_report_native import Factory

from open_deep_research.agentscope_runtime.public_findings import publish_handoff
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import UnknownOperation
from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.events.public import project_public_events

pytestmark = pytest.mark.asyncio
# ruff: noqa: F811


def handoff():
    return ResearchHandoff(
        task_id="task-one", research_topic="Evidence", requirement_ids=[],
        compressed_research="INTERNAL_MATERIAL " * 200,
        evidence_registry=[
            {"source_url": "https://example.org/source?token=hidden", "source_title": "Official"},
            {"source_url": "https://example.org/source#duplicate"},
            {"source_type": "local_document", "source_uri": "/documents/doc-one?chunk=chunk-one", "document_id": "doc-one", "chunk_id": "chunk-one"},
        ],
    )


async def test_findings_sql_replay_deduplicates_sources_and_model(store):
    state, lease = await create(store)

    class SummaryFactory(Factory):
        async def generate_structured_output(self, messages, schema):
            self.calls.append(schema.__name__)
            assert len(messages[0].get_text_content()) < 1500
            return StructuredResponse(content={"findings": ["Supported public finding."]})

    factory = SummaryFactory()
    for index in range(2):
        session = RecoverySession(store, lease, state) if index == 0 else await RecoverySession.open(store, state.run_id, "owner")
        try:
            with session.scope("research_supervisor", 0), session.task("task-one"):
                await publish_handoff(ResearchModels(factory, recovery=session), handoff(), context_chars=1000)
        finally:
            await session.close()
    events = await NativeRuns(store, None, None).events(state.run_id, "owner")
    projection = project_public_events(events)
    assert factory.calls == ["PublicFindingsSummary"]
    assert len(projection.sources) == 2
    assert len(projection.latest_findings) == 1
    assert projection.latest_findings[0]["summary"] == "- Supported public finding."
    assert projection.latest_findings[0]["source_count"] == 2
    wire = str([event.public_dict() for event in events])
    assert "INTERNAL_MATERIAL" not in wire and "token=hidden" not in wire
    assert "/documents/doc-one?chunk=chunk-one" in wire


@pytest.mark.parametrize("mode", ["rejected", "unavailable", "unknown"])
async def test_findings_failure_does_not_expose_raw_or_swallow_unknown(store, mode):
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    calls = []

    async def summary(*args):
        calls.append(1)
        raise UnknownOperation("unknown") if mode == "unknown" else RuntimeError("unavailable")

    models = SimpleNamespace(recovery=session, structured=summary)
    result = handoff()
    if mode == "rejected":
        result.assessment = {"handoff": {"admission_status": "rejected"}}
    try:
        if mode == "unknown":
            with pytest.raises(UnknownOperation):
                await publish_handoff(models, result, context_chars=1000)
        else:
            await publish_handoff(models, result, context_chars=1000)
        events = await NativeRuns(store, None, None).events(state.run_id, "owner")
        assert not any(event.type == "findings.updated" for event in events)
        assert "INTERNAL_MATERIAL" not in str(events)
        assert len(calls) == (0 if mode == "rejected" else 1)
        if mode == "rejected":
            assert not any(event.type == "research.source.discovered" for event in events)
    finally:
        await session.close()
