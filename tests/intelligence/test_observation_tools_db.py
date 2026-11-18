import json
from types import SimpleNamespace

import pytest

from open_deep_research.intelligence.store import record
from open_deep_research.intelligence.tools import observation_tools, ReadObservationInput
from open_deep_research.intelligence.observations import read_document
from open_deep_research.intelligence.contracts import IntelligenceError
from test_domain_db import capture_sequence

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_snapshot_read_is_not_evidence_and_extraction_uses_stable_operation_keys(domain, monkeypatch):
    source = await capture_sequence(domain, "A", monkeypatch)
    snapshot = record(await domain.pool.fetchrow("SELECT * FROM intelligence_snapshots WHERE source_id=$1::uuid", source))
    quote = read_document(snapshot).markdown
    calls = []
    async def structured(role, prompt, schema, state, **kwargs):
        calls.append(kwargs["operation_key"])
        chunks, _ = json.JSONDecoder().raw_decode(prompt.split("\nChunks:", 1)[1].lstrip())
        return schema(items=[{"chunk_id": chunks[0]["chunk_id"], "claim": "The price is USD 65.", "supporting_excerpt": quote, "confidence": 0.9}])
    models = SimpleNamespace(factory=None, structured=structured,
        recovery=SimpleNamespace(stage=SimpleNamespace(get=lambda: "research_supervisor"), task_id=SimpleNamespace(get=lambda: "main-research")))
    config = {"metadata": {"owner": domain.actor, "observation_manifest": {"snapshots": [snapshot]}}}
    tool = observation_tools(models, lambda: config, objective="Verify the price")[0]
    context = SimpleNamespace(tool_call_id="observation-call")
    plain = await tool.call(ReadObservationInput(snapshot_id=snapshot["id"]), context)
    assert plain.output["evidence_eligible"] is False and not calls
    for _ in range(2):
        result = await tool.call(ReadObservationInput(snapshot_id=snapshot["id"], mode="evidence"), context)
        assert result.output["evidence"][0]["observation_snapshot_id"] == snapshot["id"]
        assert result.output["evidence"][0]["source_type"] == "web"
    assert calls[0] == calls[1] and "observation-call" in calls[0]
    with pytest.raises(IntelligenceError, match="outside_frozen"):
        await tool.call(ReadObservationInput(snapshot_id="another-snapshot"), context)
