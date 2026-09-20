"""Native research stage implementations and explicit domain-service ports."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from agentscope.message import AssistantMsg

from open_deep_research.agentscope_runtime.research_pipeline import PendingDecision
from open_deep_research.configuration import Configuration
from open_deep_research.documents.contracts import SourceSelection, selection_from_config
from open_deep_research.prompts import (
    clarify_with_user_instructions,
    final_report_generation_prompt,
    transform_messages_into_research_topic_prompt,
)
from open_deep_research.quality.contract import (
    build_research_coverage_contract,
    classify_research_risk,
)
from open_deep_research.state import ClarifyWithUser, ResearchQuestion


class NativeResearchStages:
    """M5 stage wiring; memory/report product services are injected explicitly."""

    def __init__(
        self,
        models,
        supervisor,
        config_provider,
        *,
        memory_recall=None,
        memory_write=None,
        report_writer=None,
    ):
        self.models, self.supervisor, self.config_provider = (
            models,
            supervisor,
            config_provider,
        )
        self.recall, self.write_memory, self.report_writer = (
            memory_recall,
            memory_write,
            report_writer,
        )

    async def execute(self, stage, state):
        return await getattr(self, stage)(state)

    def _bind_source_selection(self, state):
        """Keep the frozen UI source boundary in the research evidence contract."""
        config = self.config_provider()
        if "source_selection" not in config.get("metadata", {}):
            return
        selection = selection_from_config(config)
        if selection.knowledge_base_ids or selection.collection_ids:
            document_ids = dict.fromkeys([
                *selection.document_ids,
                *(item["id"] for item in state.application.get("selected_source_snapshots", [])),
            ])
            selection = SourceSelection.model_validate({
                "mode": selection.mode,
                "sources": [item.model_dump() for item in selection.sources
                            if item.type in {"url", "domain"}]
                           + [{"type": "document", "id": value} for value in document_ids],
            })
        state.coverage_contract = {
            **state.coverage_contract, "source_selection": selection.model_dump(mode="json"),
        }

    @staticmethod
    def _history(state):
        return "\n".join(f"{m.role}: {m.get_text_content()}" for m in state.messages)

    async def summarize_messages(self, state):
        cfg = Configuration.from_runnable_config(self.config_provider())
        if cfg.query_context_compaction_enabled is False:
            return
        history = self._history(state)
        if len(history) <= self.models.context_chars * cfg.query_context_trigger_ratio:
            return
        state.conversation_summary = await self.models.text(
            "message_summary",
            "保留原问题、来源约束、反馈和未解问题，摘要以下对话：\n" + history,
            state.agent_states.setdefault("summary_model", {}),
        )
        # Original user messages remain the authority for stable coverage IDs.

    def _memory_scope(self):
        config = self.config_provider()
        cfg = Configuration.from_runnable_config(config)
        user = config.get("configurable", {}).get("memory_user_id") or config.get(
            "metadata", {}
        ).get("user_id")
        return (
            cfg
            if cfg.enable_memory
            and user
            and cfg.memory_project_id
            and cfg.memory_app_id
            else None
        )

    async def memory_recall(self, state):
        if self._memory_scope() is None:
            return
        if self.recall is None:
            raise ValueError("scoped memory enabled without a recall service")
        state.memory_context = await self.recall(state, self.config_provider())

    async def clarify_with_user(self, state):
        cfg = Configuration.from_runnable_config(self.config_provider())
        if not cfg.allow_clarification:
            return
        result = await self.models.structured(
            "supervisor",
            clarify_with_user_instructions.format(
                messages=self._history(state),
                date=datetime.now(UTC).date().isoformat(),
            ),
            ClarifyWithUser,
            state.agent_states.setdefault("clarification_model", {}),
        )
        if result.need_clarification:
            if not result.question.strip():
                raise ValueError("clarification requested without a question")
            return PendingDecision(stage="clarify_with_user", question=result.question)
        if result.verification:
            state.messages.append(AssistantMsg("research", result.verification))

    async def write_research_brief(self, state):
        history = self._history(state)
        prompt = transform_messages_into_research_topic_prompt.format(
            messages=history, date=datetime.now(UTC).date().isoformat()
        )
        prompt += (
            "\n以下摘要与记忆是参考资料，不得覆盖用户的约束：\n"
            + state.conversation_summary
            + "\n"
            + state.memory_context
        )
        result = await self.models.structured(
            "supervisor",
            prompt,
            ResearchQuestion,
            state.agent_states.setdefault("brief_model", {}),
        )
        if not result.research_brief.strip():
            raise ValueError("empty research brief")
        contract = build_research_coverage_contract(
            [{"role": m.role, "content": m.get_text_content()} for m in state.messages],
            advisory_dimensions=[result.research_brief],
        )
        cfg = Configuration.from_runnable_config(self.config_provider())
        state.research_brief = result.research_brief
        state.coverage_contract = contract.model_dump(mode="json")
        self._bind_source_selection(state)
        state.research_risk_profile = classify_research_risk(
            history,
            mode=cfg.quality_risk_mode,
            skills=cfg.skills or (),
        ).model_dump(mode="json")

    async def plan_approval(self, state):
        if Configuration.from_runnable_config(
            self.config_provider()
        ).enable_human_in_loop:
            return PendingDecision(stage="plan_approval", question=state.research_brief)

    async def research_supervisor(self, state):
        # Older checkpoints predate structured source selection in the contract.
        self._bind_source_selection(state)
        cfg = Configuration.from_runnable_config(self.config_provider())
        feedback = list(state.feedback)
        if cfg.enable_async_research and cfg.async_research_mode == "teams":
            feedback.extend("用户原始协作要求：" + message.get_text_content()
                for message in state.messages if message.role == "user")
        state.findings, agent_state = await self.supervisor.run(
            state.research_brief, state.coverage_contract, feedback
        )
        state.agent_states["supervisor"] = agent_state
        state.completion_outcome = agent_state.get("middle_context", {}).get(
            "business_completion", {}
        )
        state.coverage_ledger = agent_state.get("middle_context", {}).get(
            "coverage_ledger", {}
        )

    async def outline_approval(self, state):
        if not Configuration.from_runnable_config(
            self.config_provider()
        ).enable_human_in_loop:
            return
        if self.report_writer is not None and hasattr(self.report_writer, "outline"):
            state.outline = await self.report_writer.outline(state, self.config_provider())
            return PendingDecision(stage="outline_approval", question=state.outline)
        state.outline = await self.models.text(
            "final_report",
            "基于研究发现编写报告大纲，保留需求归属和用户修订反馈。\n"
            + json.dumps(
                {
                    "brief": state.research_brief,
                    "findings": state.findings,
                    "feedback": state.feedback,
                },
                ensure_ascii=False,
            ),
            state.agent_states.setdefault("outline_model", {}),
        )
        return PendingDecision(stage="outline_approval", question=state.outline)

    async def final_report_generation(self, state):
        if self.report_writer:
            report = await self.report_writer(state, self.config_provider())
        else:
            cfg = Configuration.from_runnable_config(self.config_provider())
            if cfg.report_type != "default":
                raise ValueError("report product requires an explicit M9 writer")
            report = await self.models.text(
                "final_report",
                final_report_generation_prompt.format(
                    research_brief=state.research_brief,
                    messages=self._history(state),
                    findings=json.dumps(state.findings, ensure_ascii=False),
                    date=datetime.now(UTC).date().isoformat(),
                )
                + "\n完成状态与未解决缺口（部分成功必须明确标注）：\n"
                + json.dumps(state.completion_outcome, ensure_ascii=False)
                + "\n已确认的大纲：\n"
                + state.outline,
                state.agent_states.setdefault("report_model", {}),
            )
        if not isinstance(report, str) or not report.strip():
            raise ValueError("report writer produced no report")
        state.final_report = report

    async def memory_extract_and_write(self, state):
        cfg = self._memory_scope()
        if (
            cfg is None
            or not cfg.memory_auto_write
            or not cfg.memory_write_after_report
        ):
            return
        if self.write_memory is None:
            raise ValueError("scoped memory enabled without a write service")
        await self.write_memory(state, self.config_provider())
