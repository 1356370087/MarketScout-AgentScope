"""Restic disaster recovery: consistent database/files, checksums and empty-target restore."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg


def postgres_env(dsn):
    """Build libpq environment variables without exposing passwords in process arguments."""
    url = urlsplit(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    return {
        **os.environ,
        "PGHOST": url.hostname or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGUSER": unquote(url.username or ""),
        "PGPASSWORD": unquote(url.password or ""),
        "PGDATABASE": unquote(url.path.lstrip("/")),
    }


def command(args, env=None, cwd=None):
    """Run a backup command and return output while keeping failure messages credential-free."""
    result = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        # Restic and libpq errors may contain credentials or endpoint query strings.
        raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result.stdout


def restic(*args, cwd=None):
    """Run Restic after checking required configuration."""
    required = (
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    )
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise ValueError("Missing backup configuration: " + ", ".join(missing))
    return command(["restic", *args], cwd=cwd)


def digest(path):
    """Compute a streaming SHA-256 digest."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


async def snapshot(destination):
    """Freeze document writes while copying the database snapshot and immutable blobs."""
    dsn = os.environ["DOCUMENT_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    storage = Path(os.environ["DOCUMENT_STORAGE_DIR"])
    connection = await asyncpg.connect(dsn)
    try:
        async with connection.transaction(isolation="repeatable_read"):
            tables = await connection.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND (tablename LIKE 'research_document%' OR tablename LIKE 'knowledge_%') ORDER BY tablename"
            )
            if not tables:
                raise ValueError("knowledge_schema_missing")
            await connection.execute(
                "LOCK TABLE "
                + ",".join('"' + r["tablename"] + '"' for r in tables)
                + " IN SHARE MODE"
            )
            token = await connection.fetchval("SELECT pg_export_snapshot()")
            await asyncio.to_thread(
                command,
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--snapshot=" + token,
                    "--file=" + str(destination / "database.dump"),
                ],
                postgres_env(dsn),
            )
            keys = await connection.fetch(
                "SELECT DISTINCT storage_key FROM research_document_versions"
            )
            originals = destination / "documents"
            originals.mkdir()
            for row in keys:
                source = (storage / row["storage_key"]).resolve()
                if not source.is_relative_to(storage.resolve()):
                    raise ValueError("storage_path_outside_root")
                target = originals / source.relative_to(storage.resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copyfile, source, target)
            if (storage / "previews").exists():
                await asyncio.to_thread(
                    shutil.copytree, storage / "previews", originals / "previews"
                )
            counts = {
                r["tablename"]: await connection.fetchval(
                    'SELECT count(*) FROM "' + r["tablename"] + '"'
                )
                for r in tables
            }
            revision = await connection.fetchval(
                "SELECT version_num FROM alembic_version"
            )
        files = {
            str(p.relative_to(destination).as_posix()): digest(p)
            for p in destination.rglob("*")
            if p.is_file()
        }
        (destination / "manifest.json").write_text(
            json.dumps(
                {
                    "format": "insightforge-dr-v1",
                    "revision": revision,
                    "counts": counts,
                    "files": files,
                    "created_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        await connection.close()


async def backup():
    """Write a consistent snapshot to Restic and check repository integrity."""
    started = time.monotonic()
    # Initialize explicitly with the init command; authentication errors never trigger init.
    await asyncio.to_thread(restic, "snapshots", "--json")
    with tempfile.TemporaryDirectory(prefix="insightforge-backup-") as directory:
        destination = Path(directory) / "snapshot"
        destination.mkdir()
        await snapshot(destination)
        output = await asyncio.to_thread(
            restic,
            "backup",
            "--json",
            "--tag",
            "insightforge-knowledge",
            "snapshot",
            cwd=directory,
        )
        summaries = [
            json.loads(line) for line in output.splitlines() if line.startswith("{")
        ]
        summary = next(
            item
            for item in reversed(summaries)
            if item.get("message_type") == "summary"
        )
    await asyncio.to_thread(restic, "check")
    print(  # noqa: T201 -- CLI emits a machine-readable verification result.
        json.dumps(
            {
                "status": "backed_up",
                "snapshot_id": summary["snapshot_id"],
                "seconds": round(time.monotonic() - started, 2),
            }
        )
    )


async def restore(snapshot_id, destination, database_url):
    """Restore to a fresh directory and an already-created empty database only."""
    started = time.monotonic()
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("restore_directory_must_not_exist")
    if not database_url:
        raise ValueError("restore_database_url_required")
    source = postgres_env(os.environ["DOCUMENT_DATABASE_URL"])
    target = postgres_env(database_url)
    if all(source[k] == target[k] for k in ("PGHOST", "PGPORT", "PGDATABASE")):
        raise ValueError("restore_database_must_differ_from_source")
    connection = await asyncpg.connect(
        database_url.replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        if await connection.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        ):
            raise ValueError("restore_database_must_be_empty")
        await asyncio.to_thread(
            restic,
            "restore",
            snapshot_id,
            "--tag",
            "insightforge-knowledge",
            "--target",
            str(destination),
        )
        root = destination / "snapshot"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest["format"] != "insightforge-dr-v1":
            raise ValueError("backup_manifest_invalid")
        for name, checksum in manifest["files"].items():
            path = (root / name).resolve()
            if not path.is_relative_to(root) or digest(path) != checksum:
                raise ValueError("restored_file_checksum_mismatch")
        await asyncio.to_thread(
            command,
            [
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--dbname=" + target["PGDATABASE"],
                str(root / "database.dump"),
            ],
            target,
        )
        for table, count in manifest["counts"].items():
            actual = await connection.fetchval(
                'SELECT count(*) FROM "' + table.replace('"', '""') + '"'
            )
            if actual != count:
                raise ValueError("restored_record_count_mismatch:" + table)
        if (
            await connection.fetchval("SELECT version_num FROM alembic_version")
            != manifest["revision"]
        ):
            raise ValueError("restored_schema_mismatch")
        print(  # noqa: T201 -- CLI emits a machine-readable verification result.
            json.dumps(
                {
                    "status": "restore_verified",
                    "revision": manifest["revision"],
                    "files": len(manifest["files"]),
                    "tables": len(manifest["counts"]),
                    "seconds": round(time.monotonic() - started, 2),
                }
            )
        )
    finally:
        await connection.close()


async def main():
    """Run the configured disaster-recovery command."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation", choices=["init", "backup", "loop", "check", "restore"]
    )
    parser.add_argument("--snapshot", default="latest")
    parser.add_argument("--destination")
    args = parser.parse_args()
    if args.operation == "init":
        restic("init")
    elif args.operation == "check":
        restic("check", "--read-data")
    elif args.operation == "restore":
        if not args.destination:
            raise ValueError("restore_destination_required")
        await restore(
            args.snapshot, args.destination, os.getenv("KNOWLEDGE_RESTORE_DATABASE_URL")
        )
    else:
        while True:
            await backup()
            if args.operation == "backup":
                break
            await asyncio.to_thread(
                restic,
                "forget",
                "--tag",
                "insightforge-knowledge",
                "--group-by",
                "tags",
                "--keep-daily",
                os.getenv("KNOWLEDGE_BACKUP_KEEP_DAILY", "30"),
                "--prune",
            )
            await asyncio.sleep(
                float(os.getenv("KNOWLEDGE_BACKUP_INTERVAL_HOURS", "24")) * 3600
            )


if __name__ == "__main__":
    asyncio.run(main())
