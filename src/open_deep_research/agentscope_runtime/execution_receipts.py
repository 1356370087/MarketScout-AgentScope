"""Project process evidence from a single run without exposing tool content."""

from sqlalchemy import select


async def execution_receipts(store, run_id, owner):
    progress = await store.research_progress_view(run_id, owner)
    budget = await store.budget(run_id, owner)
    table = store.outbox
    async with store.engine.connect() as conn:
        events = (await conn.execute(select(table.c.sequence, table.c.payload).where(
            table.c.run_id == run_id, table.c.payload["type"].as_string().in_(
                ["evaluation.tool_requested", "evaluation.tool_completed", "research.public"])
        ).order_by(table.c.sequence))).all()
    return summarize_receipts(run_id, progress, budget, events)


def summarize_receipts(run_id, progress, budget, events):
    completed = {e["operation_key"]: (seq, e) for seq, e in events
                 if e["type"] == "evaluation.tool_completed" and e.get("operation_key")}
    tasks, skills, gaps = {}, [], []
    skill_requests = {}
    first_web = None
    loaded = progress.get("loaded_skills", {})
    for sequence, event in events:
        if event.get("public_type") == "research.task.created":
            task = event.get("public_payload") or {}
            if task.get("task_id"):
                tasks[task["task_id"]] = task.get("mode")
        if event["type"] != "evaluation.tool_requested":
            continue
        if event.get("name") in {"fetch_url", "web_research", "web_search", "fetch_webpage"}:
            first_web = sequence if first_web is None else min(first_web, sequence)
        if event.get("name") == "ReadSkill":
            key = (event.get("args") or {}).get("key")
            op = event.get("operation_key")
            call_id = event.get("id") or op
            if not isinstance(key, str) or not key or not call_id:
                gaps.append("skill_completion")
                continue
            # The public tool boundary and SQL reservation can record the same call.
            # A completion must match a request operation, and belongs to that task and Skill.
            matches = skill_requests.setdefault((event.get("task_id"), call_id, key), {})
            done = completed.get(op)
            if done:
                matches[done[0]] = done[1]
    for (task, _, key), matches in skill_requests.items():
        if len(matches) != 1:
            gaps.append("skill_completion")
            continue
        sequence, done = next(iter(matches.items()))
        if done.get("error") or done.get("state", "committed") != "committed" or done.get("task_id", task) != task:
            gaps.append("skill_completion")
            continue
        skills.append({"key": key, "task_id": task, "sequence": sequence,
                       "version": loaded.get(f"{task}:{key}", {}).get("version")})
    skills.sort(key=lambda item: item["sequence"])
    if not tasks or set(progress.get("tasks", {})) - tasks.keys():
        gaps.append("task_topology")
    if not skills or any(not any((r["task_id"], r["key"]) == (s["task_id"], s["key"]) for r in skills)
                         for s in loaded.values()):
        gaps.append("skill_order")
    reads = list(progress.get("web_reads", {}).values())
    count = budget.get("used", {}).get("fetch_calls")
    if count is None or not reads or sum(r["physical_fetches"] for r in reads) != count:
        gaps.append("web_receipts")
    documents = document_reads(progress)
    if any(doc.get("success") and not doc.get("cached") and not doc.get("read_at") for doc in documents):
        gaps.append("read_time")
    return {"run_id": run_id, "status": "partial" if gaps else "verified", "unknown": sorted(set(gaps)),
            "tasks": tasks, "skills": skills, "first_web_sequence": first_web,
            "physical_fetches": count, "cache_reuses": sum(bool(d.get("cached")) for d in documents) if reads else None,
            "documents": documents}


def document_reads(progress):
    versions = {(doc.get("url"), doc.get("content_hash")): identity
                for identity, doc in progress.get("documents", {}).items() if doc.get("content_hash")}
    return [{**doc, **({"document_id": versions[(doc["url"], doc["content_hash"])]}
             if (doc.get("url"), doc.get("content_hash")) in versions else {})}
            for read in progress.get("web_reads", {}).values() for doc in read.get("reads", [])]


def submitted_read_receipts(selected, progress):
    """Expose signed read times for selected SQL candidate IDs, independently of tool text."""
    candidates = progress.get("candidates", {})
    versions = {(row.get("source_url"), row.get("document_id")) for view in selected
                if (row := candidates.get(view.get("evidence_id"))) and view.get("source_url") == row.get("source_url")}
    reads = {}
    for doc in document_reads(progress):
        identity = (doc.get("url"), doc.get("document_id"))
        if (identity in versions and identity[1] and doc.get("success") and not doc.get("cached") and doc.get("read_at")):
            reads[identity] = {k: doc.get(k) for k in ("url", "document_id", "content_hash", "read_at", "published_at")}
    return list(reads.values())


def bind_verified_reads(records, executions):
    """Attach the actual read time only for the same URL and same extracted document version."""
    observed = {}
    for run in executions:
        for doc in run.get("documents", []):
            if doc.get("success") and not doc.get("cached") and doc.get("read_at") and doc.get("document_id"):
                observed[(doc["url"], doc["document_id"])] = {
                    "run_id": run["run_id"], "read_at": doc["read_at"], "published_at": doc.get("published_at")}
    result = []
    for record in records:
        row = dict(record)
        row.pop("_source_read_receipt", None)
        read = observed.get((row.get("source_url"), row.get("document_id")))
        if read and row.get("source_type", "web") == "web":
            row["_source_read_receipt"] = read
        result.append(row)
    return result


async def source_run_receipts(store, parent, owner):
    """Attribute the original research and its supplemental reads to their actual runs."""
    sources = []
    while True:
        version = parent.application.get("review_derivation")
        if not version or version.get("urls"):
            receipt = await execution_receipts(store, parent.run_id, owner)
            sources.append({**receipt, "relationship": "supplement" if version else "original_research"})
        if not version:
            break
        try:
            parent, _ = await store.load(version["parent_run_id"], owner)
        except KeyError:
            sources.append({"run_id": version["parent_run_id"], "relationship": "unavailable",
                            "status": "unavailable", "physical_fetches": None})
            break
    return sources
