"""M10 archive boundaries and engine-independent HTTP contracts."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_deep_research.api.contracts import RunRequest
from open_deep_research.api.history import HistoricalRunReader, build_history_router
from open_deep_research.api.streams import StreamOptions, _public_event_iterator
from open_deep_research.events.public import PublicEvent
from open_deep_research.run_context import RunManifest, SessionJournalRecord


def archive(tmp_path):
    root = tmp_path / "old" / "context"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(
        RunManifest(
            run_id="old",
            owner_id="alice",
            status="completed",
            config={"api_key": "do-not-expose"},
            result={"status": "partial"},
        ).model_dump_json(),
        encoding="utf-8",
    )
    (root / "final_report.md").write_text("# 历史报告", encoding="utf-8")
    event = PublicEvent(
        run_id="old",
        sequence=1,
        event_id="e1",
        timestamp="2026-09-17T00:00:00Z",
        type="run.completed",
        payload={"status": "completed"},
        dedupe_key="done",
    )
    (root.parent / "public_events.jsonl").write_text(
        event.model_dump_json() + "\n", encoding="utf-8"
    )
    return root


def hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_history_read_is_owner_scoped_read_only_and_no_config_exposure(tmp_path):
    archive(tmp_path)
    before = hashes(tmp_path)
    reader = HistoricalRunReader(tmp_path, "old", "alice")
    snapshot = reader.snapshot()
    assert snapshot["output"]["markdown"] == "# 历史报告"
    assert snapshot["output"]["status"] == "partial"
    assert snapshot["last_event_id"] == 1
    assert snapshot["read_only"] and not snapshot["resumable"]
    assert "do-not-expose" not in json.dumps(snapshot)
    assert reader.budget() is None
    assert reader.events(after=1) == []
    with pytest.raises(FileNotFoundError):
        HistoricalRunReader(tmp_path, "old", "bob")
    with pytest.raises(FileNotFoundError):
        HistoricalRunReader(tmp_path, "../old", "alice")
    assert hashes(tmp_path) == before


def test_history_http_snapshot_and_resume_conflict(tmp_path):
    archive(tmp_path)
    app = FastAPI()
    app.include_router(
        build_history_router(tmp_path, lambda: SimpleNamespace(user_id="alice"))
    )
    with TestClient(app) as client:
        assert client.get("/runs/old").status_code == 200
        response = client.post("/runs/old/resume", json={})
        assert response.status_code == 409
        assert response.json()["detail"] == "legacy_checkpoint_read_only"
        assert client.post("/runs/missing/resume", json={}).status_code == 404
        replay = client.get("/runs/old/events")
        assert replay.status_code == 200
        assert "id: 1\nevent: run.completed\n" in replay.text
        assert client.get("/runs/old/events", headers={"Last-Event-ID": "1"}).text == ""
        assert client.get("/runs/old/events?after=2").status_code == 409
        assert client.get("/runs/old/events?after=-1").status_code == 400
        assert (
            client.get("/runs/old/events", headers={"Last-Event-ID": "bad"}).status_code
            == 400
        )


@pytest.mark.parametrize(
    "file", ["manifest.json", "../public_events.jsonl", "session_memory.jsonl"]
)
def test_corrupt_history_never_repairs_files(tmp_path, file):
    context = archive(tmp_path)
    (context / file).write_bytes(b"{broken")
    before = hashes(tmp_path)
    with pytest.raises(ValueError):
        reader = HistoricalRunReader(tmp_path, "old", "alice")
        reader.snapshot()
        reader.messages()
    assert hashes(tmp_path) == before


def test_history_query_message_artifact_and_override(tmp_path):
    context = archive(tmp_path)
    artifact = context / "artifacts/messages/large.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps({"message": {"type": "human", "data": {"content": "历史问题"}}}),
        encoding="utf-8",
    )
    first = SessionJournalRecord(
        seq=1,
        run_id="old",
        record_type="query_state",
        stage="query.ready",
        payload={
            "state": {
                "messages": [{"__message_artifact__": "artifacts/messages/large.json"}]
            }
        },
    )
    journal = context / "session_memory.jsonl"
    journal.write_text(first.model_dump_json() + "\n", encoding="utf-8")
    reader = HistoricalRunReader(tmp_path, "old", "alice")
    assert reader.messages().messages[0].content[0].text == "历史问题"
    second = SessionJournalRecord(
        seq=2,
        run_id="old",
        record_type="state_delta",
        stage="test",
        payload={
            "update": {
                "messages": {
                    "type": "override",
                    "value": [
                        {"__message__": {"type": "human", "data": {"content": "替换"}}}
                    ],
                }
            }
        },
    )
    journal.write_text(
        first.model_dump_json() + "\n" + second.model_dump_json() + "\n",
        encoding="utf-8",
    )
    assert reader.messages().messages[0].content[0].text == "替换"
    first.payload["state"]["messages"][0]["__message_artifact__"] = (
        "../../../outside.json"
    )
    journal.write_text(first.model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        reader.messages()


@pytest.mark.parametrize("mode", ["web", "documents", "hybrid", "specific"])
def test_shared_request_keeps_source_contract(mode):
    request = RunRequest(
        messages=[{"role": "user", "content": "query"}],
        source_selection={
            "mode": mode,
            "sources": [] if mode == "web" else [{"type": "document", "id": "doc-1"}],
        },
    )
    assert request.model_dump()["source_selection"]["mode"] == mode


def test_stream_live_reauthorization_precedes_replay():
    async def run():
        async def denied(principal):
            return False

        options = StreamOptions(
            SimpleNamespace(sse_poll_interval_ms=1, sse_heartbeat_seconds=1),
            asyncio.Event(),
            denied,
            0,
        )
        # No store is needed: revoked sessions must be stopped before reading.
        frames = [
            frame
            async for frame in _public_event_iterator(
                None, principal=SimpleNamespace(session_id="revoked"), options=options
            )
        ]
        assert frames == []

    asyncio.run(run())
