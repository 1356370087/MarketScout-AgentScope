"""Durable knowledge jobs; one transaction keeps claims and side effects atomic."""

import argparse
import asyncio
import json
import logging
import os
import uuid

from open_deep_research.documents.database import close_document_pool, get_document_pool
from open_deep_research.documents.identity import document_owner_id

from . import authz

log = logging.getLogger(__name__)


async def enqueue(c, kb, actor, kind, payload, business_key=None):
    """Insert a durable job with a unique business key."""
    return str(
        await c.fetchval(
            """INSERT INTO knowledge_jobs
        (knowledge_base_id,actor_id,kind,payload,business_key) VALUES($1::uuid,$2::uuid,$3,$4::jsonb,$5)
        ON CONFLICT(business_key) DO UPDATE SET business_key=EXCLUDED.business_key RETURNING id""",
            kb,
            document_owner_id(actor),
            kind,
            json.dumps(payload),
            business_key or str(uuid.uuid4()),
        )
    )


def artifact_dir():
    """Return the shared transfer-artifact directory."""
    from open_deep_research.documents.settings import get_document_settings

    path = get_document_settings().storage_dir / "transfers"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def run_one():
    """Claim and execute one queued or lease-expired knowledge job."""
    pool = await get_document_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow("""UPDATE knowledge_jobs SET status='running',attempts=attempts+1,
            lease_until=now()+interval '10 minutes',updated_at=now()
            WHERE id=(SELECT id FROM knowledge_jobs WHERE
                (status='queued' AND available_at<=now()) OR (status='running' AND lease_until<now())
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""")
    if not row:
        return False
    payload = (
        json.loads(row["payload"])
        if isinstance(row["payload"], str)
        else row["payload"]
    )
    job_id, attempt = str(row["id"]), row["attempts"]

    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            async with pool.acquire() as c:
                await c.execute(
                    "UPDATE knowledge_jobs SET lease_until=now()+interval '10 minutes' WHERE id=$1::uuid AND attempts=$2 AND status='running'",
                    job_id,
                    attempt,
                )

    beat = asyncio.create_task(heartbeat())
    try:
        actor, kb = str(row["actor_id"]), str(row["knowledge_base_id"])
        await authz.require_kb_capability(
            actor,
            kb,
            authz.CAP_MANAGE
            if row["kind"] in {"export", "import"}
            else authz.CAP_SUBMIT,
        )
        if row["kind"] == "extract_facts":
            from .facts import extract_candidates_for_generation

            result = {
                "items": await extract_candidates_for_generation(actor, kb, **payload)
            }
        elif row["kind"] == "export":
            from .exporter import export_to_path

            dest = artifact_dir() / f"{job_id}.zip"
            await export_to_path(actor, kb, dest)
            result = {"filename": dest.name, "size_bytes": dest.stat().st_size}
        elif row["kind"] == "import":
            from .exporter import import_archive

            result = await import_archive(
                actor, kb, artifact_dir() / payload["filename"], job_id
            )
        elif row["kind"] == "wiki_generate":
            from .wiki import generate_page

            result = await generate_page(actor, **payload)
        else:
            raise ValueError("unknown_knowledge_job")
        async with pool.acquire() as c:
            await c.execute(
                "UPDATE knowledge_jobs SET status='completed',result=$3::jsonb,error=NULL,updated_at=now() WHERE id=$1::uuid AND attempts=$2",
                job_id,
                attempt,
                json.dumps(result, default=str),
            )
    except Exception as exc:
        log.exception("Knowledge job failed: %s", job_id)
        async with pool.acquire() as c:
            await c.execute(
                """UPDATE knowledge_jobs SET status=$3,error=$4,
                available_at=now()+interval '30 seconds',updated_at=now() WHERE id=$1::uuid AND attempts=$2""",
                job_id,
                attempt,
                "failed"
                if attempt >= 3
                or isinstance(exc, ValueError | authz.AuthorizationError)
                else "queued",
                type(exc).__name__ + ":" + str(exc)[:250],
            )
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
    return True


async def main(once=False):
    """Run the configured knowledge job and citation maintenance loop."""
    try:
        while True:
            worked = await run_one()
            pool = await get_document_pool()
            async with pool.acquire() as c:
                pages = await c.fetch(
                    "SELECT id FROM knowledge_pages WHERE published_revision_id IS NOT NULL"
                )
            from .wiki import check_stale_citations

            for page in pages:
                await check_stale_citations(str(page["id"]))
            # Expired transfer artifacts are never served beyond this TTL.
            import time

            ttl = int(os.getenv("KNOWLEDGE_EXPORT_TTL_HOURS", "24")) * 3600
            for file in artifact_dir().glob("*.zip"):
                if time.time() - file.stat().st_mtime > ttl:
                    file.unlink(missing_ok=True)
            if once:
                return
            await asyncio.sleep(
                0 if worked else float(os.getenv("KNOWLEDGE_WORKER_POLL_SECONDS", "5"))
            )
    finally:
        await close_document_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    asyncio.run(main(parser.parse_args().once))
