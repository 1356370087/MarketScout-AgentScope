"""First-party source discovery, verification and versioned user decisions."""

import json
import re
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

from agentscope.message import SystemMsg, UserMsg
from pydantic import BaseModel, Field

from open_deep_research.documents.contracts import SourceSelection, normalize_source_url
from open_deep_research.quality.context import CONTEXT_RULES, research_context_xml
from open_deep_research.quality.planning import source_intent_from_user


class SourceVerification(BaseModel):
    """Quotes and observed links substantiate a first-party ownership judgment."""

    entity_quote: str = Field(default="", description="One exact contiguous substring copied from the PAGE identifying this entity. No surrounding quotation marks, summaries, explanations or stitched excerpts.")
    ownership_quote: str = Field(default="", description="One exact contiguous substring copied from the PAGE indicating first-party branding, ownership, maintainership or official documentation. No paraphrase. Empty when unsupported.")
    documentation_urls: list[str] = Field(default_factory=list, max_length=4)
    competing_websites: list[str] = Field(default_factory=list, max_length=3,
        description="Only plausible alternative official identities for the SAME named entity; its acknowledged maintainer, documentation site or product is not a competing identity.")
    reason: str = ""


def observed_links(content, website):
    """Resolve page hyperlinks without promoting links to approved evidence."""
    links = re.findall(r"https?://[^\s<>\])\"']+|(?<=\]\()[^\s)]+", content)
    result = []
    for link in links:
        try:
            result.append(normalize_source_url(urljoin(website, link)))
        except ValueError:
            continue
    return list(dict.fromkeys(result))


def verified_entry(entity, website, content, verdict, *, discovered_urls=None):
    """Require quoted page text and actual links rather than a confidence number."""
    website = normalize_source_url(website)
    links = observed_links(content, website)
    docs = []
    for url in verdict.documentation_urls:
        try:
            normalized = normalize_source_url(url)
        except ValueError:
            continue
        if normalized in links:
            docs.append(normalized)
    quotes = verdict.entity_quote and verdict.ownership_quote
    confirmed = bool(quotes and verdict.entity_quote in content and verdict.ownership_quote in content
                     and entity.casefold() in verdict.entity_quote.casefold() and docs
                     and not verdict.competing_websites)
    if discovered_urls is not None:
        confirmed = confirmed and any(urlsplit(url).hostname == urlsplit(website).hostname for url in discovered_urls)
    relevant_discoveries = [url for url in (discovered_urls or []) if urlsplit(url).hostname == urlsplit(website).hostname]
    return {"entity": entity, "website": website, "documentation_urls": docs,
            "domain": urlsplit(website).hostname, "status": "verified" if confirmed else "needs_confirmation",
            "discovery_basis": "configured-provider search and proposed website",
            "discovered_urls": relevant_discoveries,
            "verification_basis": {"entity_quote": verdict.entity_quote, "ownership_quote": verdict.ownership_quote,
                                   "observed_documentation_links": docs, "reason": verdict.reason},
            "competing_websites": verdict.competing_websites}


def apply_source_plan(contract, plan, original_selection):
    """Bind confirmed first-party ownership without broadening explicit selection."""
    from copy import deepcopy

    contract = deepcopy(contract)
    contract["source_plan"] = plan
    # Admission remains separate from ownership: explicit URL/domain choices win.
    contract["source_selection"] = original_selection
    return contract


class SourcePlanner:
    """Preparation uses run-owned native models and governed search/fetch calls."""

    def __init__(self, models, config_provider, tools_for, *, dispatcher, local_zones, run_id):
        self.models, self.config_provider, self.tools_for = models, config_provider, tools_for
        self.dispatcher, self.local_zones, self.run_id = dispatcher, local_zones, run_id

    async def prepare(self, state, brief):
        from open_deep_research.agentscope_runtime.recovery_store import digest
        from open_deep_research.agentscope_runtime.tools import prepare_toolkit
        from open_deep_research.tools.governance import AgentRole

        intent = source_intent_from_user(state.messages)
        original = "\n".join(m.get_text_content() for m in state.messages if m.role == "user")
        quote = brief.source_directive.strip()
        if intent == "unrestricted" and quote and quote in original and re.search(r"(?:使用|采用|来自|依据|只能|全部|所有|sources?|documents?).{0,80}(?:官网|官方|official)", quote, re.IGNORECASE):
            intent = brief.source_intent
        config = self.config_provider()
        selection = state.coverage_contract.get("source_selection") or config.get("metadata", {}).get("source_selection", {"mode": "web", "sources": []})
        explicit = SourceSelection.model_validate(selection)
        if not explicit.web_enabled:
            return None
        if intent == "unrestricted" and explicit.sources:
            intent = "explicit"
        from open_deep_research.evidence import compile_source_scope
        scope = compile_source_scope(state.coverage_contract)
        if not explicit.sources and scope.explicit_url_only:
            selection = SourceSelection.model_validate({"mode": "specific", "sources": [
                {"type": "url", "url": url} for url in sorted(scope.allowed_urls)]}).model_dump(mode="json")
            explicit = SourceSelection.model_validate(selection)
            if intent == "unrestricted":
                intent = "explicit"
        if intent == "unrestricted":
            return None
        version = state.application.get("source_plan", {}).get("version", 0) + 1
        if explicit.sources:
            state.application["approved_source_selection"] = selection
            state.coverage_contract["source_selection"] = selection
            return {"version": version, "intent": intent, "entries": [], "explicit": True,
                    "status": "user_specified", "selection": selection}
        prior = state.application.get("source_plan")
        if prior and prior.get("status") in {"confirmed", "automatic"}:
            return prior
        original = "\n".join(m.get_text_content() for m in state.messages if m.role == "user")
        entities = [e for e in brief.entities if e.name.casefold() in original.casefold()][:4]
        recovery = self.models.recovery
        exclusions = [r for r in state.coverage_contract.get("requirements", [])
                      if re.search(r"(?:禁止|不得|排除|不要|exclude|never).{0,80}https?://", r["text"], re.IGNORECASE)]
        preparation_contract = {"source_selection": selection, "requirements": exclusions}
        state.application["source_preparation"] = {"active": True, "task_id": "source-planning", "selection": selection,
                                                    "contract": preparation_contract}
        if recovery:
            recovery.snapshot = state
            await recovery.save(state)
        publisher = config.get("_event_publisher")
        if publisher:
            for event in ("created", "started"):
                await publisher.publish("research.task." + event, stage="planning", payload={
                    "task_id": "source-planning", "title": "官网来源准备与归属核验", "mode": "sync", "status": "pending" if event == "created" else "running"},
                    dedupe_key=f"source-planning:{version}:{event}")
        def scoped():
            return {**self.config_provider(), "metadata": {**self.config_provider().get("metadata", {}),
                    "task_id": "source-planning", "source_selection": selection, "coverage_contract": preparation_contract}}
        tools = await self.tools_for(SimpleNamespace(task_id="source-planning"))
        toolkit = await prepare_toolkit([t for t in tools if t.name in {"source_discovery", "fetch_url"}],
            role=AgentRole.RESEARCHER, config_provider=scoped, run_id=self.run_id,
            task_id="source-planning", local_zones=self.local_zones, dispatcher=self.dispatcher)
        toolkit.journal = recovery
        outcomes = []
        async def observe(name, call_id, result):
            outcomes.append(result.result.output if result.result else {})
        toolkit.result_observer = observe
        async def call(name, args):
            cid = digest(["source-plan", version, name, args])
            with recovery.task("source-planning") if recovery else nullcontext():
                result = await toolkit.call_domain_tool(name, args, cid)
            if result.metadata.get("error_type"):
                return ""
            return outcomes[-1] if outcomes else ""
        entries = []
        for entity in entities:
            discoveries = await call("source_discovery", {"queries": [entity.name + " official website documentation"]})
            try:
                hits = json.loads(discoveries)["candidates"]
            except (ValueError, TypeError, KeyError):
                hits = []
            hits = [h for h in hits if entity.name.casefold() in " ".join(str(h.get(k, "")) for k in ("title", "snippet", "canonical_url")).casefold()]
            proposed = entity.website
            if not proposed:
                try:
                    proposed = hits[0]["canonical_url"] if hits else ""
                except (ValueError, TypeError, KeyError):
                    proposed = ""
            if not proposed:
                entries.append({"entity": entity.name, "status": "needs_confirmation", "website": "", "documentation_urls": [], "reason": "未发现可核验官网"})
                continue
            try:
                proposed = normalize_source_url(proposed)
            except ValueError:
                entries.append({"entity": entity.name, "status": "needs_confirmation", "website": "", "documentation_urls": [], "reason": "候选官网地址无效"})
                continue
            raw = await call("fetch_url", {"url": proposed, "mode": "markdown", "max_chars": 16000,
                "objective": f"核验 {entity.name} 官网归属及链接到的官方文档；准备资料不能作为报告证据"})
            content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            try:
                page = json.loads(content)
                content = page.get("markdown", content) if isinstance(page, dict) else content
            except ValueError:
                pass
            verdict = await self.models.structured("supervisor", "", SourceVerification, {"task_id": "source-planning"},
                messages=[
                    SystemMsg("system", CONTEXT_RULES +
                        "Verify ownership of the proposed website from the supplied PAGE text. Quote verbatim evidence of entity and ownership; list only documentation links literally present in the page. Search snippets are hints, not proof. Competing plausible official identities require confirmation. Missing evidence means empty fields."),
                    UserMsg("user", research_context_xml(payload={"entity": entity.name, "website": proposed, "discoveries": hits,
                        "observed_links": observed_links(content, proposed), "page": content}))],
                purpose="source_ownership_verification")
            entries.append(verified_entry(entity.name, proposed, content, verdict,
                discovered_urls=[h["canonical_url"] for h in hits if h.get("canonical_url")]))
        state.application["source_preparation"]["active"] = False
        if recovery:
            from open_deep_research.configuration import Configuration
            from open_deep_research.sandbox.approvals import SecurityApprovalStore
            expired = SecurityApprovalStore(self.run_id, runs_dir=Configuration.from_runnable_config(config).runs_dir).expire_task(
                "source-planning", fence_token=recovery.lease.fence)
            if publisher:
                for item in expired:
                    await publisher.publish("security.approval.resolved", stage="planning", payload={
                        "approval_id": item.approval_id, "task_id": item.task_id, "kind": item.kind,
                        "capability": item.capability, "decision": "deny", "status": "expired", "version": item.version},
                        dedupe_key=f"source-planning-expired:{item.approval_id}:{item.version}")
        if publisher:
            await publisher.publish("research.task.completed", stage="planning", payload={"task_id": "source-planning", "status": "completed"},
                dedupe_key=f"source-planning:{version}:completed")
        return {"version": version, "intent": intent, "entries": entries, "explicit": False,
                "status": "verified" if entries and all(e["status"] == "verified" for e in entries) else "needs_confirmation",
                "selection": selection}
