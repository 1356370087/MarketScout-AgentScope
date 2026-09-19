"""Run T061 real Restic/pg_dump/pg_restore on disposable databases and files."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import asyncpg


def command(args, **kwargs):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, **kwargs
    ).stdout


async def seed(storage):
    from open_deep_research.documents import corrections, repository, versioning
    from open_deep_research.documents.chunking import DocumentChunk
    from open_deep_research.documents.database import (
        close_document_pool,
        get_document_pool,
    )
    from open_deep_research.documents.settings import get_document_settings
    from open_deep_research.documents.storage import StagedUpload

    owner = str(uuid4())
    content = b"Verified backup reference: revenue 42 million."
    path = storage / "original.txt"
    path.write_bytes(content)
    document, _ = await repository.create_document(
        owner,
        StagedUpload(
            path,
            path.name,
            "text/plain",
            len(content),
            hashlib.sha256(content).hexdigest(),
        ),
        path.name,
        get_document_settings(),
    )
    generation = (await versioning.list_generations(owner, document.id))[0]["id"]
    chunk = DocumentChunk(
        id=str(uuid4()),
        ordinal=0,
        locator="section:1",
        heading="Revenue",
        text=content.decode(),
        content_hash=hashlib.sha256(content).hexdigest(),
    )
    await versioning.complete_generation(
        generation,
        [chunk],
        [[0.1] * 1536],
        embedding_model="fixture",
        page_count=1,
        ocr_pages=0,
    )
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE research_document_jobs SET status='completed' WHERE document_id=$1::uuid",
            document.id,
        )
    await corrections.apply_corrections(
        owner,
        document.id,
        generation,
        revision=0,
        metadata_confirmed={"doc_type": "测试资料"},
    )
    await versioning.publish_generation(owner, document.id, generation)
    row = await repository.get_document(owner, document.id)
    await repository.bind_run_sources("backup-acceptance", owner, [row])
    await close_document_pool()
    return document.id, generation, hashlib.sha256(content).hexdigest()


async def verify(dsn, expected):
    connection = await asyncpg.connect(dsn)
    try:
        row = await connection.fetchrow(
            """SELECT s.document_id, s.generation_id, v.storage_key, v.sha256
               FROM research_run_sources s
               JOIN research_document_generations g ON g.id=s.generation_id
               JOIN research_document_versions v ON v.id=g.version_id
               WHERE s.run_id='backup-acceptance'"""
        )
        assert (
            str(row["document_id"]),
            str(row["generation_id"]),
            row["sha256"],
        ) == expected
        return row["storage_key"]
    finally:
        await connection.close()


def main():
    root = Path(__file__).resolve().parents[2]
    evidence = root / "tmp" / ("m8-backup-" + uuid4().hex[:8])
    storage = evidence / "documents"
    storage.mkdir(parents=True)
    name = "as-m8-backup-" + uuid4().hex[:8]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    source = f"postgresql://postgres:probe@127.0.0.1:{port}/postgres"
    target = source.rsplit("/", 1)[0] + "/restored"
    command(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=probe",
            "-p",
            f"127.0.0.1:{port}:5432",
            "pgvector/pgvector:pg17",
        ]
    )
    try:
        for _ in range(45):
            if (
                subprocess.run(
                    ["docker", "exec", name, "pg_isready", "-U", "postgres"],
                    capture_output=True,
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        os.environ.update(
            DOCUMENT_RESEARCH_ENABLED="true",
            DOCUMENT_DATABASE_URL=source,
            DOCUMENT_STORAGE_DIR=str(storage),
            IAM_DATABASE_URL=source.replace("postgresql://", "postgresql+asyncpg://"),
        )
        command([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root)
        expected = asyncio.run(seed(storage))
        command(["docker", "exec", name, "createdb", "-U", "postgres", "restored"])
        base = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{evidence}:/evidence",
            "-v",
            f"{root / 'src/open_deep_research/knowledge/backup.py'}:/app/backup.py:ro",
        ]
        for key, value in {
            "DOCUMENT_DATABASE_URL": source.replace(
                "127.0.0.1", "host.docker.internal"
            ),
            "KNOWLEDGE_RESTORE_DATABASE_URL": target.replace(
                "127.0.0.1", "host.docker.internal"
            ),
            "DOCUMENT_STORAGE_DIR": "/evidence/documents",
            "RESTIC_REPOSITORY": "/evidence/repository",
            "RESTIC_PASSWORD": "isolated-acceptance",
            "AWS_ACCESS_KEY_ID": "unused-local-repository",
            "AWS_SECRET_ACCESS_KEY": "unused-local-repository",
        }.items():
            base += ["-e", f"{key}={value}"]
        base += ["insight_forge-knowledge-backup:latest"]
        outputs = {
            "init": command([*base, "init"]),
            "backup": command([*base, "backup"]),
        }
        outputs["restore"] = command(
            [*base, "restore", "--destination", "/evidence/restored"]
        )
        key = asyncio.run(verify(target, expected))
        assert (
            hashlib.sha256(
                (evidence / "restored/snapshot/documents" / key).read_bytes()
            ).hexdigest()
            == expected[2]
        )
        result = {
            "status": "passed",
            "reference_chain_verified": True,
            "outputs": outputs,
        }
        (evidence / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": "passed",
                    "evidence": str(evidence),
                    "reference_chain_verified": True,
                }
            )
        )
    finally:
        command(["docker", "rm", "-f", name])


if __name__ == "__main__":
    main()
