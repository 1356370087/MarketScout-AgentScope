"""Two-step purge of pre-knowledge-base local-document test data.

Default invocation only previews what a purge would remove. Passing
``--execute`` first re-runs the guards (no live document worker, no active
research run bound to the material) and only then deletes document-module
rows and their original blobs. IAM data, other tables and ``.runs`` are never
touched; file deletion is restricted to keys resolved inside the configured
document storage directory.

Usage::

    uv run python -m open_deep_research.documents.cleanup            # preview
    uv run python -m open_deep_research.documents.cleanup --execute
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .database import get_document_pool
from .settings import get_document_settings
from .storage import delete_storage_key, resolve_storage_key

_TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)


@dataclass
class CleanupPreview:
    """Everything the purge would remove plus the guard verdicts."""

    documents: int = 0
    chunks: int = 0
    jobs: int = 0
    run_sources: int = 0
    live_workers: int = 0
    active_runs: list[str] = field(default_factory=list)
    storage_keys: list[tuple[str, Path, int]] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)

    @property
    def guards_ok(self) -> bool:
        """Return whether both operational guards currently pass."""
        return not self.live_workers and not self.active_runs


def _run_status(runs_dir: Path, run_id: str) -> str | None:
    """Read one run manifest's status; None means no run data on disk."""
    manifest = runs_dir / run_id / "context" / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("status", ""))
    except (OSError, ValueError):
        # An unreadable manifest cannot prove the run is inactive.
        return "unreadable"


async def collect_preview(runs_dir: Path) -> CleanupPreview:
    """Gather counts, storage targets and guard verdicts without deleting."""
    settings = get_document_settings()
    preview = CleanupPreview()
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        counts = await connection.fetchrow(
            """SELECT (SELECT count(*) FROM research_documents) AS documents,
                      (SELECT count(*) FROM research_document_chunks) AS chunks,
                      (SELECT count(*) FROM research_document_jobs) AS jobs,
                      (SELECT count(*) FROM research_run_sources) AS run_sources"""
        )
        preview.documents = int(counts["documents"])
        preview.chunks = int(counts["chunks"])
        preview.jobs = int(counts["jobs"])
        preview.run_sources = int(counts["run_sources"])
        preview.live_workers = int(
            await connection.fetchval(
                """SELECT count(*) FROM research_document_worker_heartbeats
                    WHERE heartbeat_at >= now()-make_interval(secs=>$1)""",
                max(1.0, settings.worker_heartbeat_seconds * 3),
            )
            or 0
        )
        run_ids = await connection.fetch(
            "SELECT DISTINCT run_id FROM research_run_sources"
        )
        keys = await connection.fetch(
            "SELECT DISTINCT storage_key FROM research_documents"
        )
    for row in run_ids:
        status = _run_status(runs_dir, str(row["run_id"]))
        if status is None or status in _TERMINAL_RUN_STATUSES:
            continue
        preview.active_runs.append(f"{row['run_id']}({status})")
    for row in keys:
        try:
            path = resolve_storage_key(str(row["storage_key"]), settings)
        except Exception:  # noqa: BLE001 - report the key instead of aborting
            preview.missing_files.append(str(row["storage_key"]))
            continue
        if path.is_file():
            preview.storage_keys.append((str(row["storage_key"]), path, path.stat().st_size))
        else:
            preview.missing_files.append(str(row["storage_key"]))
    return preview


async def execute_cleanup(preview: CleanupPreview) -> dict[str, int]:
    """Delete document-module rows, then original blobs outside the transaction."""
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        # research_run_sources holds the RESTRICT reference; the remaining
        # document rows cascade their chunks, jobs and knowledge links.
        await connection.execute("DELETE FROM research_run_sources")
        removed = await connection.fetchval(
            "WITH removed AS (DELETE FROM research_documents RETURNING 1) "
            "SELECT count(*) FROM removed"
        )
    freed_bytes = 0
    deleted_blobs = 0
    for key, path, size in preview.storage_keys:
        delete_storage_key(key, get_document_settings())
        if not path.exists():
            deleted_blobs += 1
            freed_bytes += size
    return {
        "documents": int(removed or 0),
        "blobs": deleted_blobs,
        "freed_bytes": freed_bytes,
    }


def _print_preview(preview: CleanupPreview) -> None:
    print("Local-document purge preview")  # noqa: T201
    print(f"  documents:     {preview.documents}")  # noqa: T201
    print(f"  chunks:        {preview.chunks}")  # noqa: T201
    print(f"  jobs:          {preview.jobs}")  # noqa: T201
    print(f"  run bindings:  {preview.run_sources}")  # noqa: T201
    total_bytes = sum(size for _, _, size in preview.storage_keys)
    print(f"  storage keys:  {len(preview.storage_keys)} ({total_bytes} bytes on disk)")  # noqa: T201
    for key, path, size in preview.storage_keys:
        print(f"    {size:>12}  {path}")  # noqa: T201
    if preview.missing_files:
        print(f"  keys without a resolvable file: {len(preview.missing_files)}")  # noqa: T201
        for key in preview.missing_files:
            print(f"    {key}")  # noqa: T201
    print(f"  live document workers: {preview.live_workers}")  # noqa: T201
    print(f"  active runs bound to material: {len(preview.active_runs)}")  # noqa: T201
    for run in preview.active_runs:
        print(f"    {run}")  # noqa: T201


def main(argv: list[str] | None = None) -> int:
    """Run the preview or the guarded purge; returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m open_deep_research.documents.cleanup",
        description="Purge pre-knowledge-base local-document test data.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually delete after re-running the guards (default: preview only)",
    )
    parser.add_argument(
        "--runs-dir",
        default=".runs",
        help="runs directory used to check whether bound research runs are active",
    )
    args = parser.parse_args(argv)

    settings = get_document_settings()
    if not settings.configured:
        print("document_research_not_configured: set DOCUMENT_DATABASE_URL", file=sys.stderr)  # noqa: T201
        return 1

    async def run() -> tuple[CleanupPreview, dict[str, int] | None]:
        preview = await collect_preview(Path(args.runs_dir).resolve())
        _print_preview(preview)
        if not args.execute:
            return preview, None
        if not preview.guards_ok:
            return preview, None
        result = await execute_cleanup(preview)
        return preview, result

    preview, result = asyncio.run(run())
    if not args.execute:
        print("\nPreview only. Re-run with --execute after stopping the document worker.")  # noqa: T201
        return 0
    if not preview.guards_ok:
        print(  # noqa: T201
            "\nPurge refused: stop every document worker and let the listed runs "
            "finish (or cancel them) before retrying.",
            file=sys.stderr,
        )
        return 2
    print(  # noqa: T201
        f"\nPurged {result['documents']} documents and {result['blobs']} original "
        f"blobs ({result['freed_bytes']} bytes freed). IAM data and .runs untouched."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
