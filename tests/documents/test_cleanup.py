"""Purge command behaviour: preview first, guarded execution second."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from open_deep_research.documents import cleanup

OWNER = "b4f63627-81d8-461e-95c6-d26471c0b570"
DOC_ID = "11111111-1111-1111-1111-111111111111"


class CleanupConnection:
    """Dispatches the four preview queries and records destructive SQL."""

    def __init__(self, *, documents=1, chunks=3, jobs=1, run_sources=1,
                 live_workers=0, run_ids=(), storage_keys=()):
        self.documents = documents
        self.chunks = chunks
        self.jobs = jobs
        self.run_sources = run_sources
        self.live_workers = live_workers
        self.run_ids = list(run_ids)
        self.storage_keys = list(storage_keys)
        self.executed: list[str] = []

    async def fetchrow(self, sql, *args):
        assert "count(*)" in sql
        return {"documents": self.documents, "chunks": self.chunks,
                "jobs": self.jobs, "run_sources": self.run_sources}

    async def fetchval(self, sql, *args):
        if "worker_heartbeats" in sql:
            return self.live_workers
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetch(self, sql, *args):
        if "DISTINCT run_id" in sql:
            return [{"run_id": item} for item in self.run_ids]
        if "DISTINCT storage_key" in sql:
            return [{"storage_key": item} for item in self.storage_keys]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def execute(self, sql, *args):
        self.executed.append(sql)

    async def fetchval_removed(self, sql, *args):  # pragma: no cover - clarity
        raise AssertionError

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield

    def prepare_for_execute(self):
        self._removed = self.documents
        connection = self

        async def fetchval(sql, *args):
            if "WITH removed" in sql:
                connection.executed.append(sql)
                return connection._removed
            if "worker_heartbeats" in sql:
                return connection.live_workers
            raise AssertionError(f"unexpected fetchval: {sql}")

        self.fetchval = fetchval  # type: ignore[method-assign]


def _pool(connection):
    async def get_pool():
        return _BarePool(connection)

    return get_pool


class _BarePool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def _configure(monkeypatch, tmp_path, connection):
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", "postgresql://localhost:5432/docs")
    monkeypatch.setenv("DOCUMENT_STORAGE_DIR", str(storage))
    monkeypatch.setattr(cleanup, "get_document_pool", _pool(connection))
    return storage


def _write_blob(storage: Path, key: str, size: int = 128) -> Path:
    path = storage / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _write_run_manifest(runs_dir: Path, run_id: str, status: str) -> None:
    manifest = runs_dir / run_id / "context" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"status": status}), encoding="utf-8")


def test_preview_deletes_nothing(monkeypatch, tmp_path, capsys):
    storage_key = "ownerhash/ab/blob.pdf"
    connection = CleanupConnection(storage_keys=[storage_key])
    storage = _configure(monkeypatch, tmp_path, connection)
    blob = _write_blob(storage, storage_key)
    runs_dir = tmp_path / "runs"

    assert cleanup.main(["--runs-dir", str(runs_dir)]) == 0
    assert blob.is_file()
    assert connection.executed == []
    output = capsys.readouterr().out
    assert "Preview only" in output
    assert str(blob) in output


def test_execute_removes_rows_and_files(monkeypatch, tmp_path, capsys):
    storage_key = "ownerhash/ab/blob.pdf"
    connection = CleanupConnection(run_ids=["run-done"], storage_keys=[storage_key])
    connection.prepare_for_execute()
    storage = _configure(monkeypatch, tmp_path, connection)
    blob = _write_blob(storage, storage_key)
    runs_dir = tmp_path / "runs"
    _write_run_manifest(runs_dir, "run-done", "completed")

    assert cleanup.main(["--runs-dir", str(runs_dir), "--execute"]) == 0
    assert not blob.exists()
    assert any("DELETE FROM research_run_sources" in sql for sql in connection.executed)
    assert any("DELETE FROM research_documents" in sql for sql in connection.executed)
    assert "Purged 1 documents" in capsys.readouterr().out


def test_execute_refused_while_worker_heartbeats(monkeypatch, tmp_path, capsys):
    connection = CleanupConnection(live_workers=1, storage_keys=["ownerhash/ab/blob.pdf"])
    connection.prepare_for_execute()
    storage = _configure(monkeypatch, tmp_path, connection)
    blob = _write_blob(storage, "ownerhash/ab/blob.pdf")

    assert cleanup.main(["--runs-dir", str(tmp_path / "runs"), "--execute"]) == 2
    assert blob.is_file()
    assert connection.executed == []
    assert "Purge refused" in capsys.readouterr().err


def test_execute_refused_while_run_is_active(monkeypatch, tmp_path):
    connection = CleanupConnection(run_ids=["run-live"])
    connection.prepare_for_execute()
    _configure(monkeypatch, tmp_path, connection)
    runs_dir = tmp_path / "runs"
    _write_run_manifest(runs_dir, "run-live", "running")

    assert cleanup.main(["--runs-dir", str(runs_dir), "--execute"]) == 2
    assert connection.executed == []


def test_execute_treats_unreadable_manifest_as_active(monkeypatch, tmp_path):
    connection = CleanupConnection(run_ids=["run-corrupt"])
    connection.prepare_for_execute()
    _configure(monkeypatch, tmp_path, connection)
    manifest = tmp_path / "runs" / "run-corrupt" / "context" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{not json", encoding="utf-8")

    assert cleanup.main(["--runs-dir", str(tmp_path / "runs"), "--execute"]) == 2


def test_unconfigured_environment_fails_fast(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DOCUMENT_DATABASE_URL", raising=False)
    monkeypatch.delenv("IAM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DOCUMENT_RESEARCH_ENABLED", raising=False)
    assert cleanup.main([]) == 1
    assert "document_research_not_configured" in capsys.readouterr().err
