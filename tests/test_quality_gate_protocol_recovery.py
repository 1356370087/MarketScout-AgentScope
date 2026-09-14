"""Regression tests for the handoff Judge protocol-recovery chain.

Covers the 4th E2E failure (run 478bf6b6): the Judge returned top scores and
"all satisfied" prose while ``requirement_coverage`` was empty, the gate
force-rejected with un-actionable reasons, and the Supervisor stalled because
no requirement-specific remediation ever reached it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from open_deep_research.completion import (
    CompletionPolicyContext,
    ResearchCompletionPolicy,
)
from open_deep_research.quality.contract import AdmissionStatus
from open_deep_research.quality.gate import (
    HandoffAssessment,
    _deterministic_follow_up_tasks,
    _handoff_protocol_errors,
)
from open_deep_research.quality.policy import get_run_quality_rigor_policy


def _policy():
    return get_run_quality_rigor_policy(
        "balanced", policy_version="quality-gate-v4"
    )


def _checks() -> dict:
    return {"passed": True, "failures": []}


def _self_contradictory_assessment() -> HandoffAssessment:
    return HandoffAssessment(
        accepted=True,
        admission_status=AdmissionStatus.ACCEPTED,
        relevance=5,
        source_quality=5,
        evidence_coverage=5,
        groundedness=5,
        reason="所有需求均已满足。",
        requirement_coverage=[],
    )


class TestProtocolValidatorOwnsCoverage:
    def test_missing_owned_coverage_is_a_protocol_error(self):
        errors = _handoff_protocol_errors(
            _self_contradictory_assessment(),
            checks=_checks(),
            policy=_policy(),
            exclusive_requirement_ids=("COV-1", "COV-2"),
        )
        assert "requirement_coverage_missing:COV-1,COV-2" in errors

    def test_complete_owned_coverage_passes_without_that_error(self):
        base = _self_contradictory_assessment()
        assessment = HandoffAssessment(
            **{
                **base.model_dump(
                    exclude={"requirement_coverage"}
                ),
                "requirement_coverage": [
                    {
                        "requirement_id": "COV-1",
                        "status": "supported",
                        "evidence_ids": ["ev-1"],
                        "explanation": "",
                    },
                    {
                        "requirement_id": "COV-2",
                        "status": "partial",
                        "evidence_ids": [],
                        "explanation": "",
                    },
                ],
            }
        )
        errors = _handoff_protocol_errors(
            assessment,
            checks=_checks(),
            policy=_policy(),
            exclusive_requirement_ids=("COV-1", "COV-2"),
        )
        assert not [
            error
            for error in errors
            if error.startswith("requirement_coverage_missing")
        ]

    def test_no_owned_ids_disables_the_check(self):
        errors = _handoff_protocol_errors(
            _self_contradictory_assessment(),
            checks=_checks(),
            policy=_policy(),
            exclusive_requirement_ids=(),
        )
        assert not [
            error
            for error in errors
            if error.startswith("requirement_coverage_missing")
        ]


class TestDeterministicFollowUpTasks:
    def test_hard_rejections_become_requirement_specific_tasks(self):
        contract = SimpleNamespace(
            requirements=(
                SimpleNamespace(
                    requirement_id="COV-9",
                    text="确认 Python 3.13 的发布日期",
                ),
            )
        )
        tasks = _deterministic_follow_up_tasks(
            [
                "required_coverage_missing:COV-9",
                "owned_requirements_missing",
            ],
            resolved_contract=contract,
        )
        assert any("COV-9" in task and "发布日期" in task for task in tasks)
        assert any("requirement_ids" in task for task in tasks)

    def test_unknown_coverage_rows_map_to_reevaluation_tasks(self):
        tasks = _deterministic_follow_up_tasks(
            ["unknown_requirement_coverage:COV-2"],
            resolved_contract=None,
        )
        assert tasks and "COV-2" in tasks[0]

    def test_empty_reasons_yield_empty_tasks(self):
        assert _deterministic_follow_up_tasks([], resolved_contract=None) == []


class TestCompletionGapsCarryRequirementIds:
    def test_uncovered_requirements_surface_ids_in_gaps(self):
        decision = ResearchCompletionPolicy().evaluate(
            CompletionPolicyContext(
                evidence_count=5,
                independent_source_count=5,
                uncovered_requirements=("COV-1", "COV-2"),
                has_remaining_budget=False,
                exhausted_reason="max_turns",
            )
        )
        assert "coverage_gaps:COV-1,COV-2" in decision.gaps

    def test_covered_requirements_do_not_add_gap(self):
        decision = ResearchCompletionPolicy().evaluate(
            CompletionPolicyContext(
                explicit_completion_succeeded=True,
                evidence_count=5,
                independent_source_count=5,
                uncovered_requirements=(),
                has_remaining_budget=False,
                exhausted_reason="max_turns",
            )
        )
        assert not [
            gap for gap in decision.gaps if gap.startswith("coverage_gaps")
        ]


class TestCollectOutputsCarryRequirementIds:
    @pytest.mark.asyncio
    async def test_completed_outputs_include_requirement_ids(self):
        from open_deep_research.tasks.async_tools import collect_completed_task_outputs
        from open_deep_research.tasks.registry import TaskRegistry, TaskStatus

        registry = TaskRegistry()
        record = registry.create(
            "topic A",
            requirement_ids=["COV-1", "COV-2"],
        )
        registry.update_status(record.task_id, TaskStatus.COMPLETED)

        outputs = await collect_completed_task_outputs(registry)

        assert outputs and outputs[0]["requirement_ids"] == ["COV-1", "COV-2"]


class TestCollectIdempotency:
    @pytest.mark.asyncio
    async def test_already_adjudicated_tasks_are_not_recollected(self):
        from open_deep_research.tasks.async_tools import collect_completed_task_outputs
        from open_deep_research.tasks.registry import TaskRegistry, TaskStatus
        from open_deep_research.tasks.state import TaskSnapshot

        registry = TaskRegistry()
        fresh = registry.create(
            "fresh task", requirement_ids=["COV-1"]
        )
        done = registry.create(
            "already evaluated", requirement_ids=["COV-2"]
        )
        registry.update_status(fresh.task_id, TaskStatus.COMPLETED)
        registry.update_status(done.task_id, TaskStatus.COMPLETED)

        class Store:
            async def list(self, status_filter=None, run_id=None):
                return [
                    TaskSnapshot(
                        task_id=fresh.task_id,
                        status=TaskStatus.COMPLETED,
                        admission_status="pending",
                        requirement_ids=["COV-1"],
                    ),
                    TaskSnapshot(
                        task_id=done.task_id,
                        status=TaskStatus.COMPLETED,
                        admission_status="rejected",
                        requirement_ids=["COV-2"],
                    ),
                ]

        outputs = await collect_completed_task_outputs(
            registry, run_id="run-x", state_store=Store()
        )
        assert [output["task_id"] for output in outputs] == [fresh.task_id]


class TestFinalizeRegistryDedup:
    def test_duplicate_evidence_ids_collapse(self):
        from open_deep_research.agents.deep_researcher import _dedup_registry_items

        items = [
            {"evidence_id": "ev-1", "claim": "a"},
            {"evidence_id": "ev-2", "claim": "b"},
            {"evidence_id": "ev-1", "claim": "a"},
        ]
        assert [
            item["evidence_id"]
            for item in _dedup_registry_items(items, ("evidence_id",))
        ] == ["ev-1", "ev-2"]

    def test_items_without_identity_key_dedup_by_content(self):
        from open_deep_research.agents.deep_researcher import _dedup_registry_items

        items = [{"url": "u", "n": 1}, {"url": "u", "n": 1}, {"url": "u", "n": 2}]
        assert len(_dedup_registry_items(items, ())) == 2


class TestExclusiveOwnershipHardGate:
    def _policy_input(self, coverage_rows=(), accepted=True):
        from open_deep_research.quality.contract import HandoffPolicyInput

        return HandoffPolicyInput(
            requested_status=(
                AdmissionStatus.ACCEPTED if accepted else AdmissionStatus.REJECTED
            ),
            requirement_coverage=tuple(coverage_rows),
            caveats=(),
            missing_information=(),
            unsupported_claims=(),
            deterministic_checks_passed=True,
            scores=(5, 5, 5, 5),
            dimension_floor=3,
            average_floor=3.0,
            caveat_admission_enabled=True,
            high_risk=False,
        )

    def test_shared_requirement_does_not_hard_reject(self):
        from open_deep_research.quality.contract import (
            CoverageStatus,
            RequirementCoverage,
            resolve_handoff_admission,
        )

        coverage = [
            RequirementCoverage(
                requirement_id="COV-3", status=CoverageStatus.SUPPORTED
            ),
            RequirementCoverage(
                requirement_id="COV-4", status=CoverageStatus.PARTIAL
            ),
        ]
        result = resolve_handoff_admission(
            self._policy_input(coverage),
            owned_requirement_ids=["COV-3", "COV-4"],
            hard_requirement_ids=["COV-3"],
        )
        assert result.accepted is True
        assert not [
            reason
            for reason in result.hard_rejection_reasons
            if reason.startswith("required_coverage_missing")
        ]

    def test_exclusive_requirement_still_hard_rejects(self):
        from open_deep_research.quality.contract import resolve_handoff_admission

        result = resolve_handoff_admission(
            self._policy_input(coverage_rows=[]),
            owned_requirement_ids=["COV-3", "COV-4"],
            hard_requirement_ids=["COV-3"],
        )
        assert "required_coverage_missing:COV-3" in result.hard_rejection_reasons
        assert "required_coverage_missing:COV-4" not in (
            result.hard_rejection_reasons
        )

    @pytest.mark.asyncio
    async def test_viable_sibling_softens_shared_ids(self, monkeypatch):
        from open_deep_research.quality import gate
        from open_deep_research.tasks.registry import TaskStatus
        from open_deep_research.tasks.state import TaskSnapshot

        class Store:
            async def list(self, run_id=None, status_filter=None):
                return [
                    TaskSnapshot(
                        task_id="sibling-1",
                        status=TaskStatus.RUNNING,
                        requirement_ids=["COV-4"],
                    )
                ]

        monkeypatch.setattr(
            "open_deep_research.tasks.state.get_task_state_store",
            lambda _c: Store(),
        )
        config = {
            "configurable": {},
            "metadata": {"run_id": "run-a"},
        }
        result = await gate._exclusive_requirement_ids(
            ("COV-3", "COV-4"),
            config=config,
            configurable=None,
            handoff={"task_id": "self-1"},
        )
        assert result == ("COV-3",)

    @pytest.mark.asyncio
    async def test_failed_sibling_keeps_obligation(self, monkeypatch):
        from open_deep_research.quality import gate
        from open_deep_research.tasks.registry import TaskStatus
        from open_deep_research.tasks.state import TaskSnapshot

        class Store:
            async def list(self, run_id=None, status_filter=None):
                return [
                    TaskSnapshot(
                        task_id="sibling-1",
                        status=TaskStatus.FAILED,
                        requirement_ids=["COV-4"],
                    )
                ]

        monkeypatch.setattr(
            "open_deep_research.tasks.state.get_task_state_store",
            lambda _c: Store(),
        )
        config = {
            "configurable": {},
            "metadata": {"run_id": "run-a"},
        }
        result = await gate._exclusive_requirement_ids(
            ("COV-3", "COV-4"),
            config=config,
            configurable=None,
            handoff={"task_id": "self-1"},
        )
        assert result == ("COV-3", "COV-4")

    @pytest.mark.asyncio
    async def test_completed_rejected_sibling_keeps_obligation(
        self, monkeypatch
    ):
        """A rejected handoff merges no coverage rows, so it stays non-viable.

        Regression for the 2026-09-03 review P0: a remediation task reusing
        the COV of a completed-but-rejected sibling used to lose its only
        hard requirement and could be accepted with zero coverage rows.
        """
        from open_deep_research.quality import gate
        from open_deep_research.tasks.registry import TaskStatus
        from open_deep_research.tasks.state import TaskSnapshot

        class Store:
            async def list(self, run_id=None, status_filter=None):
                return [
                    TaskSnapshot(
                        task_id="sibling-1",
                        status=TaskStatus.COMPLETED,
                        admission_status="rejected",
                        requirement_ids=["COV-4"],
                    )
                ]

        monkeypatch.setattr(
            "open_deep_research.tasks.state.get_task_state_store",
            lambda _c: Store(),
        )
        config = {
            "configurable": {},
            "metadata": {"run_id": "run-a"},
        }
        result = await gate._exclusive_requirement_ids(
            ("COV-3", "COV-4"),
            config=config,
            configurable=None,
            handoff={"task_id": "self-1"},
        )
        assert result == ("COV-3", "COV-4")

    @pytest.mark.asyncio
    async def test_completed_accepted_sibling_softens_shared_ids(
        self, monkeypatch
    ):
        from open_deep_research.quality import gate
        from open_deep_research.tasks.registry import TaskStatus
        from open_deep_research.tasks.state import TaskSnapshot

        class Store:
            async def list(self, run_id=None, status_filter=None):
                return [
                    TaskSnapshot(
                        task_id="sibling-1",
                        status=TaskStatus.COMPLETED,
                        admission_status="accepted",
                        requirement_ids=["COV-4"],
                    )
                ]

        monkeypatch.setattr(
            "open_deep_research.tasks.state.get_task_state_store",
            lambda _c: Store(),
        )
        config = {
            "configurable": {},
            "metadata": {"run_id": "run-a"},
        }
        result = await gate._exclusive_requirement_ids(
            ("COV-3", "COV-4"),
            config=config,
            configurable=None,
            handoff={"task_id": "self-1"},
        )
        assert result == ("COV-3",)


class TestDeliverableKindClassification:
    def test_e2e_requirement_texts_classify_correctly(self):
        from open_deep_research.quality.contract import (
            classify_requirement_kind,
            is_delegable_requirement,
        )

        cases = [
            ("给出面向小型团队的中文选型建议", "deliverable"),
            ("附可核验的来源链接", "deliverable"),
            ("基于至少三个公开可靠来源", "process"),
            ("给出推荐结论与理由", "deliverable"),
        ]
        for text, expected in cases:
            assert classify_requirement_kind(text) == expected, text
            assert (
                is_delegable_requirement(
                    {
                        "requirement_id": "COV-X",
                        "text": text,
                        "kind": expected,
                    }
                )
                is False
            )

    def test_factual_dimensions_stay_delegable(self):
        from open_deep_research.quality.contract import (
            classify_requirement_kind,
            is_delegable_requirement,
        )

        for text in (
            "SQLite 与 PostgreSQL 的并发写入行为",
            "PostgreSQL 在小型 SaaS 项目中的备份恢复",
            "确认 Python 3.13 的发布日期",
        ):
            assert classify_requirement_kind(text) == "factual", text
            assert (
                is_delegable_requirement(
                    {
                        "requirement_id": "COV-X",
                        "text": text,
                        "kind": "factual",
                    }
                )
                is True
            )


class TestValidatorOnlyFlagsContradiction:
    def test_accepted_without_coverage_still_flags(self):
        errors = _handoff_protocol_errors(
            _self_contradictory_assessment(),
            checks=_checks(),
            policy=_policy(),
            exclusive_requirement_ids=("COV-1",),
        )
        assert "requirement_coverage_missing:COV-1" in errors

    def test_rejected_without_coverage_does_not_burn_retries(self):
        assessment = HandoffAssessment(
            accepted=False,
            admission_status=AdmissionStatus.REJECTED,
            relevance=3,
            source_quality=3,
            evidence_coverage=2,
            groundedness=3,
            reason="Evidence gaps remain.",
            missing_information=["gap"],
            requirement_coverage=[],
        )
        errors = _handoff_protocol_errors(
            assessment,
            checks=_checks(),
            policy=_policy(),
            exclusive_requirement_ids=("COV-1",),
        )
        assert not [
            error
            for error in errors
            if error.startswith("requirement_coverage_missing")
        ]


class TestFollowUpsAreExecutable:
    def test_every_deterministic_task_is_actionable(self):
        from open_deep_research.quality.gate import _deterministic_follow_up_tasks

        contract = SimpleNamespace(
            requirements=(
                SimpleNamespace(
                    requirement_id="COV-9", text="确认扩展边界"
                ),
            )
        )
        tasks = _deterministic_follow_up_tasks(
            [
                "required_coverage_missing:COV-9",
                "unknown_requirement_coverage:COV-9",
                "owned_requirements_missing",
                "deterministic_checks_failed",
                "unsupported_claims",
                "score_below_dimension_floor",
                "quality_evaluator_failed_closed",
            ],
            resolved_contract=contract,
        )
        assert tasks
        for task in tasks:
            actionable = ("搜索" in task) or ("不派发研究任务" in task)
            assert actionable, f"指令不可执行: {task}"

    def test_evaluator_fallbacks_do_not_request_artifact_retrieval(self):
        import inspect

        from open_deep_research.quality import gate

        source = inspect.getsource(gate)
        assert "reassess_sha_verified_artifact" not in source


class TestExclusiveScopingInPayload:
    @pytest.mark.asyncio
    async def test_payload_and_prompt_carry_exclusive_shared_split(self, monkeypatch):
        from open_deep_research.quality import gate
        from open_deep_research.tasks.registry import TaskStatus
        from open_deep_research.tasks.state import TaskSnapshot

        captured = {}

        async def fake_evaluate_json(schema, system_prompt, payload, config, **kwargs):
            captured["prompt"] = system_prompt
            captured["payload"] = payload
            from open_deep_research.quality.contract import (
                CoverageStatus,
                RequirementCoverage,
            )

            return gate.HandoffAssessment(
                accepted=True,
                admission_status=AdmissionStatus.ACCEPTED,
                relevance=5,
                source_quality=5,
                evidence_coverage=5,
                groundedness=5,
                reason="ok",
                requirement_coverage=[
                    RequirementCoverage(
                        requirement_id="COV-3",
                        status=CoverageStatus.SUPPORTED,
                        evidence_ids=[],
                        explanation="",
                    )
                ],
            )

        class Store:
            async def list(self, run_id=None, status_filter=None):
                return [
                    TaskSnapshot(
                        task_id="sibling",
                        status=TaskStatus.RUNNING,
                        requirement_ids=["COV-4"],
                    )
                ]

        monkeypatch.setattr(gate, "_evaluate_json", fake_evaluate_json)
        monkeypatch.setattr(
            "open_deep_research.tasks.state.get_task_state_store",
            lambda _c: Store(),
        )
        config = {
            "configurable": {
                "quality_evaluation_enabled": True,
                "enable_async_research": True,
            },
            "metadata": {
                "run_id": "run-split",
                "task_id": "self",
                "quality_policy_version": "quality-gate-v4",
            },
        }
        contract = {
            "requirements": [
                {
                    "requirement_id": "COV-3",
                    "text": "SQLite 并发写入",
                    "kind": "factual",
                    "source_message_index": 0,
                    "source_start": 0,
                    "source_end": 10,
                },
                {
                    "requirement_id": "COV-4",
                    "text": "PostgreSQL 并发写入",
                    "kind": "factual",
                    "source_message_index": 0,
                    "source_start": 0,
                    "source_end": 10,
                },
            ],
            "original_query_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            "advisory_dimensions": [],
        }
        await gate.evaluate_subagent_handoff(
            "topic",
            {
                "task_id": "self",
                "compressed_research": "summary",
                "raw_notes": [],
                "evidence_registry": [],
                "web_research_iterations": [
                    {
                        "gap_analysis": {
                            "budget": {
                                "fetch_attempts": 0,
                                "fetched_documents": 0,
                                "reserved_fetches": 0,
                                "exhaustion_scope": "run",
                            },
                        }
                    }
                ],
                "completion_decision": {
                    "reason": "fetch_budget_exhausted"
                },
            },
            config,
            coverage_contract=contract,
            requirement_ids=["COV-3", "COV-4"],
        )
        assert captured["payload"]["exclusive_requirement_ids"] == ["COV-3"]
        assert captured["payload"]["shared_requirement_ids"] == ["COV-4"]
        assert captured["payload"]["worker_budget_telemetry"] == {
            "web_iteration_count": 1,
            "zero_fetch_budget_exhausted_iteration_count": 1,
            "trailing_zero_fetch_budget_exhausted_streak": 1,
            "reported_fetch_attempts": 0,
            "reported_fetched_documents": 0,
            "reported_transport_failed_fetches": 0,
            "trailing_exhaustion_scope": "run",
            "completion_reason": "fetch_budget_exhausted",
        }
        assert "shared_requirement_ids" in captured["prompt"]
        assert "never lower evidence_coverage" in captured["prompt"]
        assert "Do not invent a source-authority rejection" in captured["prompt"]

    def test_validator_ignores_shared_row_absence(self):
        errors = _handoff_protocol_errors(
            _self_contradictory_assessment(),
            checks=_checks(),
            policy=_policy(),
            exclusive_requirement_ids=(),
        )
        assert not [
            error
            for error in errors
            if error.startswith("requirement_coverage_missing")
        ]


class TestSearchProviderExhausted:
    def test_error_code_classifies_quota_failures(self):
        from open_deep_research.tools.web_research.pipeline import _search_error_code

        class Forbidden(Exception):
            status_code = 403

        assert (
            _search_error_code(Forbidden("quota exceeded"))
            == "search_provider_exhausted"
        )
        assert (
            _search_error_code(Exception("API key credits exhausted"))
            == "search_provider_exhausted"
        )
        assert (
            _search_error_code(Exception("connection reset mid-flight"))
            == "Exception"
        )

    @pytest.mark.asyncio
    async def test_streak_counts_trailing_zero_source_exhausted_tasks(self, monkeypatch):
        from open_deep_research.agents import deep_researcher as dr
        from open_deep_research.tasks.registry import TaskStatus
        from open_deep_research.tasks.state import TaskSnapshot

        def snap(tid, completed, source_count, exhausted):
            iterations = (
                [{"errors": ["tavily:search_provider_exhausted:403"]}]
                if exhausted
                else []
            )
            return TaskSnapshot(
                task_id=tid,
                status=TaskStatus.COMPLETED,
                completed_at=completed,
                metrics={"source_count": source_count},
                result={"web_research_iterations": iterations},
            )

        class Store:
            async def list(self, status_filter=None, run_id=None):
                return [
                    snap("t1", 300, 0, True),
                    snap("t2", 200, 0, True),
                    snap("t3", 100, 5, True),
                    snap("t4", 50, 0, True),
                ]

        monkeypatch.setattr(
            dr,
            "get_task_state_store",
            lambda _c: Store(),
        )
        config = {
            "configurable": {"enable_async_research": True, "runs_dir": "x"},
            "metadata": {"run_id": "r"},
        }
        assert await dr.search_provider_exhausted_streak(config) == 2

    def test_policy_terminates_on_exhaustion_streak(self):
        decision = ResearchCompletionPolicy().evaluate(
            CompletionPolicyContext(
                evidence_count=0,
                search_provider_exhausted_streak=3,
            )
        )
        assert decision.reason == "search_provider_exhausted"

    def test_policy_ignores_short_streak(self):
        decision = ResearchCompletionPolicy().evaluate(
            CompletionPolicyContext(
                evidence_count=5,
                independent_source_count=5,
                explicit_completion_succeeded=True,
                search_provider_exhausted_streak=2,
            )
        )
        assert decision.action.value != "terminate" or (
            decision.reason != "search_provider_exhausted"
        )


class TestFetchBudgetExhausted:
    @pytest.mark.asyncio
    async def test_task_streak_recognizes_worker_budget_wall(self, monkeypatch):
        from open_deep_research.agents import deep_researcher as dr
        from open_deep_research.tasks.registry import TaskStatus
        from open_deep_research.tasks.state import TaskSnapshot

        class Store:
            exhaustion_scope = "run"

            async def list(self, status_filter=None, run_id=None):
                del status_filter, run_id
                exhausted = {
                    "gap_analysis": {
                        "budget": {
                            "fetch_attempts": 0,
                            "fetched_documents": 0,
                            "reserved_fetches": 0,
                            "exhaustion_scope": self.exhaustion_scope,
                        },
                    }
                }
                return [
                    TaskSnapshot(
                        task_id="task-budget-wall",
                        status=TaskStatus.COMPLETED,
                        completed_at=100,
                        metrics={"source_count": 88},
                        result={
                            "evidence_registry": [],
                            "web_research_iterations": [
                                exhausted,
                                exhausted,
                                exhausted,
                            ],
                            "completion_decision": {
                                "reason": "fetch_budget_exhausted",
                                "scope": self.exhaustion_scope,
                            },
                        },
                    )
                ]

        monkeypatch.setattr(dr, "get_task_state_store", lambda _c: Store())
        config = {
            "configurable": {"enable_async_research": True, "runs_dir": "x"},
            "metadata": {"run_id": "r"},
        }

        assert await dr.fetch_budget_exhausted_task_streak(config) == 1
        config["metadata"]["fetch_budget_extension"] = {"extra_fetches": 40, "granted_at": 101}
        assert await dr.fetch_budget_exhausted_task_streak(config) == 0
        config["metadata"].pop("fetch_budget_extension")

        Store.exhaustion_scope = "task"
        assert await dr.fetch_budget_exhausted_task_streak(config) == 0

    def test_policy_terminates_after_one_worker_reports_budget_wall(self):
        decision = ResearchCompletionPolicy().evaluate(
            CompletionPolicyContext(
                evidence_count=5,
                independent_source_count=2,
                fetch_budget_exhausted_task_streak=1,
            )
        )

        assert decision.reason == "fetch_budget_exhausted"
