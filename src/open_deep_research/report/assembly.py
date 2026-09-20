"""Evidence-grounded one-shot and concurrent sectioned report assembly."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol

from open_deep_research import prompts as _prompts
from open_deep_research.configuration import Configuration
from open_deep_research.evidence import (
    contract_has_source_constraints,
    source_scoped_evidence_records,
)
from open_deep_research.prompts import (
    final_section_writer_prompt,
    report_outline_planner_prompt,
    section_writer_prompt,
)
from open_deep_research.skills import get_skill_report_context

from .coverage import render_state_coverage_checklist
from .models import ReportOutline, SectionSpec, SourceRef, WrittenSection
from .profiles import AssemblyMode, ReportProfile
from .runtime import BaseMessage, RunnableConfig, get_buffer_string, get_today_str, require_report_runtime
from .writing import (
    order_evidence,
    project_evidence,
    writing_messages,
)


def _build_findings(state: dict) -> str:
    """Aggregate supervisor notes + completed task outputs into a findings blob.

    Verbatim port of the original aggregation in ``final_report_generation``.
    """
    notes = state.get("notes", [])
    task_outputs = state.get("completed_task_outputs", [])
    if task_outputs:
        task_findings = "\n\n".join(
            f"## Research Task: {op.get('research_topic', 'Unknown')}\n\n{op.get('compressed_research', '')}"
            for op in task_outputs
            if op.get("compressed_research")
        )
        supervisor_notes = "\n".join(notes)
        return (
            f"{supervisor_notes}\n\n{task_findings}" if supervisor_notes else task_findings
        )
    return "\n".join(notes)


def _build_scoped_evidence_findings(
    records: list[dict[str, Any]],
) -> str:
    """Project source-scoped evidence without admitting free-form handoff text."""
    findings: list[str] = []
    for index, record in enumerate(records, 1):
        evidence_id = str(
            record.get("evidence_id") or f"evidence-{index}"
        )[:200]
        claim = str(record.get("claim") or "").strip()[:2_000]
        excerpt = str(
            record.get("supporting_excerpt") or ""
        ).strip()[:3_000]
        title = str(record.get("source_title") or "Source").strip()[:300]
        url = str(record.get("source_url") or "").strip()
        locator = str(record.get("locator") or "").strip()[:500]
        lines = [
            f"## Evidence {evidence_id}",
            f"Claim: {claim}",
            f"Source: [{title}]({url})" if url else f"Source: {title}",
        ]
        if locator:
            lines.append(f"Locator: {locator}")
        if excerpt:
            lines.append(f"Supporting excerpt: {excerpt}")
        findings.append("\n".join(lines))
    return "\n\n".join(findings)


@dataclass
class AssemblyResult:
    """The output of an assembly strategy, before output-format rendering."""

    body_markdown: str
    message: Any = None  # AIMessage to append to state messages (model output or error msg)
    sections: List[WrittenSection] = field(default_factory=list)
    sources: List[SourceRef] = field(default_factory=list)


@dataclass
class ReportContext:
    """Everything an assembly strategy needs, derived from state + config + profile."""

    state: dict
    config: RunnableConfig
    profile: ReportProfile
    configurable: Configuration
    findings: str
    sources: List[SourceRef]
    evidence_records: list[dict[str, Any]] = field(default_factory=list)
    requirement_to_evidence: dict[str, list[str]] = field(default_factory=dict)

    @property
    def strict_evidence(self) -> bool:
        """Whether free-form notes must not authorize factual writing."""
        return (
            self.configurable.quality_evaluation_enabled
            or self.configurable.web_pipeline_mode == "enforced"
            or contract_has_source_constraints(self.state.get("coverage_contract"))
        )

    def stage_messages(self, template, payload, records=None):
        """Build a stage from trusted template instructions and separate data."""
        payload = {
            **payload,
            "approved_outline": self.state.get("report_outline", ""),
            "completion_outcome": self.state.get("completion_outcome", {}),
            "coverage": render_state_coverage_checklist(self.state),
            "requirement_to_evidence": self.requirement_to_evidence,
            "evidence_mode": "accepted_records" if self.strict_evidence else "historical_notes_compatibility",
        }
        if records is None:
            records = (
                order_evidence(self.evidence_records, self.requirement_to_evidence)
                if self.strict_evidence or self.evidence_records
                else [{"historical_note": note} for note in self.findings.split("\n\n") if note]
            )
        return writing_messages(
            template, payload, records,
            guidance=get_skill_report_context(self.configurable.skills),
        )

    @classmethod
    def from_state(
        cls,
        state: dict,
        config: RunnableConfig,
        profile: ReportProfile,
        sources: Optional[List[SourceRef]] = None,
    ) -> ReportContext:
        """Build report context from graph state and runnable configuration."""
        configurable = Configuration.from_runnable_config(config)
        coverage_contract = state.get("coverage_contract")
        raw_evidence = state.get("evidence_registry", [])
        if (
            isinstance(raw_evidence, dict)
            and raw_evidence.get("type") == "override"
        ):
            raw_evidence = raw_evidence.get("value", [])
        scoped_evidence = source_scoped_evidence_records(
            raw_evidence if isinstance(raw_evidence, list) else [],
            coverage_contract,
        )
        evidence_sources: list[SourceRef] = []
        seen_urls: set[str] = set()
        for record in scoped_evidence:
            url = str(record.get("source_uri") or record.get("source_url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            evidence_sources.append(
                SourceRef(
                    title=str(record.get("source_title", "")),
                    url=url,
                    source_type=(
                        str(record.get("source_type"))
                        if record.get("source_type")
                        else "local_document"
                        if record.get("document_id")
                        else None
                    ),
                    document_id=(
                        str(record["document_id"])
                        if record.get("document_id")
                        else None
                    ),
                    chunk_id=(
                        str(record["chunk_id"]) if record.get("chunk_id") else None
                    ),
                    locator=(
                        str(record["locator"]) if record.get("locator") else None
                    ),
                )
            )
        projected = project_evidence(scoped_evidence)
        accepted_ids = {record.get("evidence_id") for record in projected}
        ledger = state.get("coverage_ledger") or {}
        if ledger.get("type") == "override":
            ledger = ledger.get("value") or {}
        bindings = {
            str(key): [eid for eid in (value.get("evidence_ids") or []) if eid in accepted_ids]
            for key, value in ledger.items() if isinstance(value, dict)
        }
        for record in projected:
            for requirement_id in record.get("requirement_ids") or []:
                ids = bindings.setdefault(str(requirement_id), [])
                if record.get("evidence_id") and record["evidence_id"] not in ids:
                    ids.append(record["evidence_id"])
        return cls(
            state=state,
            config=config,
            profile=profile,
            configurable=configurable,
            findings=(
                _build_scoped_evidence_findings(scoped_evidence)
                if (configurable.quality_evaluation_enabled
                    or configurable.web_pipeline_mode == "enforced"
                    or contract_has_source_constraints(coverage_contract))
                else _build_findings(state)
            ),
            sources=list(sources or evidence_sources),
            evidence_records=projected,
            requirement_to_evidence=bindings,
        )

    @property
    def prompt_template(self):
        """Resolve the profile's prompt-constant name to the actual template."""
        return getattr(_prompts, self.profile.prompt_template)


    async def invoke_writer_with_output_recovery(self, messages: list[BaseMessage], *, span_name: str) -> BaseMessage:
        """Write through the native model policy, budget and recovery ledger."""
        return await require_report_runtime().invoke("final_report", messages, self.configurable, span_name=span_name)

    async def invoke_structured_with_fallback(self, schema, messages: list[BaseMessage], *, span_name: str):
        """Use the same native candidate policy for structured report stages."""
        return await require_report_runtime().invoke("final_report", messages, self.configurable, span_name=span_name, schema=schema)


class OneShotStrategy:
    """Single-call synthesis with candidate-aware evidence budgeting."""

    async def build(self, ctx: ReportContext) -> AssemblyResult:
        """Build a report without mixing runtime data into instructions."""
        messages = ctx.stage_messages(ctx.prompt_template, {
            "research_brief": ctx.state.get("research_brief", ""),
            "messages": get_buffer_string(ctx.state.get("messages", [])),
            "conversation_summary": ctx.state.get("conversation_summary", ""),
            "memory_context": ctx.state.get("memory_context", ""),
            "date": get_today_str(),
        })
        final_report = await ctx.invoke_writer_with_output_recovery(
            messages, span_name="lead.final_report",
        )
        return AssemblyResult(
            body_markdown=str(final_report.content), message=final_report,
            sources=ctx.sources,
        )


def _skeleton_text(skeleton) -> str:
    """Render a profile's section_skeleton hint into planner-prompt prose."""
    if skeleton:
        return "Suggested section structure (you may adapt): " + ", ".join(skeleton) + "."
    return "You may choose the section structure that best fits the topic."


def _sanitize_span(name: str) -> str:
    """Make a section name safe to embed in an observability span name."""
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")[:40] or "section"


_MAX_SECTIONS = 6


async def _gather_writes(coroutines, concurrency: int):
    """Preserve order and drain sibling tasks on failure or cancellation."""
    semaphore = asyncio.Semaphore(concurrency)

    async def run(coroutine):
        async with semaphore:
            return await coroutine()

    tasks = [asyncio.create_task(run(coroutine)) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class SectionedStrategy:
    """Plan coverage, write bounded independent sections, then frame the report."""

    async def build(self, ctx: ReportContext) -> AssemblyResult:
        """Build through outline, concurrent sections, and concurrent framing."""
        outline = await self._plan_outline(ctx)
        sections = await self._write_sections(ctx, outline)
        intro, conclusion = await _gather_writes([
            lambda: self._write_final_section(ctx, sections, "Introduction"),
            lambda: self._write_final_section(ctx, sections, "Conclusion"),
        ], ctx.configurable.report_section_concurrency)
        body = self._assemble(ctx, outline, sections, intro, conclusion)
        return AssemblyResult(body_markdown=body, sections=sections, sources=ctx.sources)

    async def _plan_outline(self, ctx: ReportContext) -> ReportOutline:
        messages = ctx.stage_messages(report_outline_planner_prompt, {
            "research_brief": ctx.state.get("research_brief", ""),
            "section_skeleton": _skeleton_text(ctx.profile.section_skeleton),
            "date": get_today_str(),
        })
        outline = await ctx.invoke_structured_with_fallback(
            ReportOutline, messages, span_name="lead.report_outline",
        )
        outline.sections = list(outline.sections)[:_MAX_SECTIONS]
        contract = ctx.state.get("coverage_contract") or {}
        if contract.get("type") == "override":
            contract = contract.get("value") or {}
        required = [r["requirement_id"] for r in contract.get("requirements", [])]
        assigned = set()
        for section in outline.sections:
            section.requirement_ids = list(dict.fromkeys(
                rid for rid in section.requirement_ids if rid in required
            ))
            assigned.update(section.requirement_ids)
        missing = [rid for rid in required if rid not in assigned]
        if not outline.sections or missing:
            if len(outline.sections) < _MAX_SECTIONS:
                outline.sections.append(SectionSpec(
                    name="研究发现" if self._chinese(ctx) else "Findings",
                    description="Address uncovered requirements, explicitly stating evidence gaps.",
                    requirement_ids=missing,
                ))
            else:
                outline.sections[-1].requirement_ids.extend(missing)
        return outline

    async def _write_sections(self, ctx: ReportContext, outline: ReportOutline):
        async def write(section):
            ids = {eid for rid in section.requirement_ids
                   for eid in ctx.requirement_to_evidence.get(rid, [])}
            # Unbound sections retain diverse evidence instead of guessing relevance.
            records = [r for r in ctx.evidence_records if r.get("evidence_id") in ids] if ids else None
            if records is not None:
                records = order_evidence(records, {
                    rid: ctx.requirement_to_evidence.get(rid, [])
                    for rid in section.requirement_ids
                })
            messages = ctx.stage_messages(section_writer_prompt, {
                "topic": ctx.state.get("research_brief", ""),
                "outline": outline.model_dump(),
                "section_name": section.name,
                "section_description": section.description,
                "requirement_ids": section.requirement_ids,
                "date": get_today_str(),
            }, records)
            resp = await ctx.invoke_writer_with_output_recovery(
                messages, span_name=f"lead.section.{_sanitize_span(section.name)}",
            )
            return WrittenSection(name=section.name, content=str(resp.content))

        return await _gather_writes(
            [lambda section=section: write(section) for section in outline.sections],
            ctx.configurable.report_section_concurrency,
        )

    async def _write_final_section(self, ctx: ReportContext, sections, section_type: str) -> str:
        records = [{"section": section.name, "content": section.content} for section in sections]
        messages = ctx.stage_messages(final_section_writer_prompt, {
            "topic": ctx.state.get("research_brief", ""),
            "section_type": section_type,
            "section_names": [section.name for section in sections],
            "date": get_today_str(),
        }, records)
        resp = await ctx.invoke_writer_with_output_recovery(
            messages, span_name=f"lead.{section_type.lower()}",
        )
        return str(resp.content)

    @staticmethod
    def _chinese(ctx) -> bool:
        import re
        return ctx is not None and bool(re.search(r"[\u4e00-\u9fff]", str(ctx.state.get("research_brief", ""))))

    def _assemble(self, ctx, outline, sections, intro, conclusion) -> str:
        intro_title, toc_title, conclusion_title = (
            ("引言", "目录", "结论") if self._chinese(ctx)
            else ("Introduction", "Table of Contents", "Conclusion")
        )
        lines = [f"# {outline.title}", ""]
        if intro:
            lines += [f"## {intro_title}", "", intro.strip(), ""]
        if sections:
            lines += [f"## {toc_title}", ""]
            for i, section in enumerate(sections, 1):
                lines.append(f"{i}. {section.name}")
            lines.append("")
        for section in sections:
            lines += [f"## {section.name}", "", section.content.strip(), ""]
        if conclusion:
            lines += [f"## {conclusion_title}", "", conclusion.strip(), ""]
        return "\n".join(lines).rstrip() + "\n"


class AssemblyStrategy(Protocol):
    """Structural interface implemented by report assembly strategies."""

    async def build(self, ctx: ReportContext) -> AssemblyResult:
        """Build an assembled report from context."""
        ...


# Strategy dispatch table.
_STRATEGIES: dict[AssemblyMode, type[AssemblyStrategy]] = {
    AssemblyMode.ONE_SHOT: OneShotStrategy,
    AssemblyMode.SECTIONED: SectionedStrategy,
}


async def assemble(ctx: ReportContext) -> AssemblyResult:
    """Dispatch to the strategy selected by the profile's assembly mode."""
    strategy_cls = _STRATEGIES.get(ctx.profile.assembly)
    if strategy_cls is None:
        raise NotImplementedError(
            f"Assembly mode '{ctx.profile.assembly.value}' is not implemented yet"
        )
    return await strategy_cls().build(ctx)
