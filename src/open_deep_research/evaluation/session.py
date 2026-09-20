"""Independent native Judge journal with bounded service-key model access."""

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from datetime import UTC, datetime

from pydantic import SecretStr

from open_deep_research.agentscope_runtime.models import CredentialBinding, ModelFactory
from open_deep_research.agentscope_runtime.recovery import (
    RecoverySession,
    RecoveryStages,
)
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.configuration import Configuration, freeze_run_config
from open_deep_research.models.catalog import (
    LiteLLMModelCatalogClient,
    freeze_catalog_snapshot,
    validate_model_catalog,
)

from .judge import JudgeConfig, evaluation_date
from .native import complete_metric_categories, evaluate_output
from .paired import fingerprint


async def create_judge_run(store, owner, run_id, run):
    """Service-key evaluations enforce their own cost cap in the SQL ledger."""
    from open_deep_research.budgets import BudgetDimension, budget_policy_from_config

    cfg = Configuration(**{name: run.get(name) for name in Configuration.model_fields})
    policy = budget_policy_from_config(cfg)
    limits = {
        item.value: policy.limit_for(item)
        for item in BudgetDimension
        if policy.limit_for(item) is not None
    }
    # Ordinary LiteLLM research delegates monetary limits to a per-run key.
    # The offline Judge uses a service key and therefore needs a local cap.
    limits[BudgetDimension.COST_MICRO_USD.value] = cfg.max_run_cost_micro_usd
    await store.create_run(
        owner,
        ResearchSnapshot(
            run_id=run_id,
            config_fingerprint=run.compatibility_projection()["metadata"][
                "run_config_fingerprint"
            ],
            application={"purpose": "evaluation"},
        ),
        limits=limits,
        deadline=time.time() + cfg.run_deadline_seconds,
    )


def rubric_fingerprint():
    from tests import evaluators, prompts

    return fingerprint(
        [
            Path(module.__file__).read_text(encoding="utf-8")
            for module in (evaluators, prompts)
        ]
    )


def judge_run_config(judge, catalog):
    """Freeze explicit Judge isolation while retaining the stricter run budgets."""
    cfg = Configuration.from_runnable_config(None)
    values = cfg.model_dump(mode="json")
    values.update(
        quality_evaluation_model=judge.model,
        quality_evaluation_model_max_tokens=min(
            judge.max_tokens, catalog[judge.model]["max_output_tokens"]
        ),
        quality_evaluation_temperature=0,
        sandbox_enabled=False,
        model_backend="litellm",
        model_fallbacks={},
        model_catalog_snapshot=catalog,
        max_run_model_calls=min(cfg.max_run_model_calls or 60, 60),
        max_run_cost_micro_usd=min(cfg.max_run_cost_micro_usd or 1_000_000, 1_000_000),
        run_deadline_seconds=min(cfg.run_deadline_seconds or 1800, 1800),
        enable_memory=False,
        enable_async_research=False,
    )
    return RunConfig.compile(
        freeze_run_config({"configurable": values}, prefer_configurable=True)
    )


class NativeJudgeSession:
    def __init__(self, models, recovery, manifest):
        self.models, self.recovery, self.manifest = models, recovery, manifest

    async def score(
        self, *, case_id, sample_id, inputs, outputs, reference_outputs=None
    ):
        request_hash = fingerprint([inputs, outputs, reference_outputs or {}])
        key = fingerprint([case_id, sample_id])
        persisted = self.recovery.snapshot.application.setdefault("evaluations", {})
        if key in persisted:
            if persisted[key]["input_sha256"] != request_hash:
                raise ValueError("evaluation_input_changed")
            return complete_metric_categories(persisted[key]["metrics"])

        session = self

        class ScoreStage:
            async def execute(self, stage, state):
                date_token = evaluation_date.set(session.manifest["evaluation_date"])
                try:
                    metrics = await evaluate_output(
                        session.models,
                        case_id=case_id,
                        sample_id=sample_id,
                        inputs=inputs,
                        outputs=outputs,
                        reference_outputs=reference_outputs,
                    )
                finally:
                    evaluation_date.reset(date_token)
                state.application.setdefault("evaluations", {})[key] = {
                    "input_sha256": request_hash,
                    "metrics": metrics,
                }
                await session.recovery.save(state)
                return metrics

        return await RecoveryStages(ScoreStage(), self.recovery).execute(
            "evaluation:" + key,
            self.recovery.snapshot,
        )


@asynccontextmanager
async def native_judge_session(directory, *, judge=None):
    """Open only an evaluation journal; never alter a source research checkpoint."""
    judge = judge or JudgeConfig.from_env()
    if not judge.api_key or not judge.base_url:
        raise ValueError("evaluation_requires_litellm_service_key_and_base_url")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run = RunConfig.restore(manifest["configuration"])
        if judge.model != run.get("quality_evaluation_model"):
            raise ValueError("evaluation_judge_model_changed")
        if manifest["rubric_sha256"] != rubric_fingerprint():
            raise ValueError("evaluation_rubric_changed")
    else:
        client = LiteLLMModelCatalogClient(
            base_url=judge.base_url, api_key=judge.api_key
        )
        try:
            catalog = await client.load()
        finally:
            await client.aclose()
        validate_model_catalog(catalog, [judge.model], budget_enabled=True)
        run = judge_run_config(judge, freeze_catalog_snapshot(catalog, [judge.model]))
        manifest = {
            "schema_version": 1,
            "run_id": uuid4().hex,
            "engine": "agentscope",
            "configuration": run.snapshot(),
            "rubric_sha256": rubric_fingerprint(),
            "evaluation_date": datetime.now(UTC).date().isoformat(),
            "judge_sha256": fingerprint(
                {
                    "model": judge.model,
                    "catalog": run.get("model_catalog_snapshot"),
                    "temperature": 0,
                    "max_tokens": run.get("quality_evaluation_model_max_tokens"),
                }
            ),
        }
        with manifest_path.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
    store = RecoveryStore(
        "sqlite+aiosqlite:///" + (directory.resolve() / "judge.db").as_posix()
    )
    recovery = factory = None
    try:
        await store.create_tables()
        owner, run_id = "offline-evaluation", manifest["run_id"]
        try:
            await store.load(run_id, owner)
        except KeyError:
            await create_judge_run(store, owner, run_id, run)
        budget = await store.budget(run_id, owner)
        if budget["limits"].get("cost_micro_usd") != run.get("max_run_cost_micro_usd"):
            raise ValueError("evaluation_cost_policy_changed_use_new_directory")
        recovery = await RecoverySession.open(store, run_id, owner)
        binding = CredentialBinding(
            "evaluation-service",
            "service",
            owner,
            (judge.model,),
            SecretStr(judge.api_key),
            judge.base_url,
            True,
        )
        factory = ModelFactory(
            run, scope="service", owner=owner, bindings={"quality_evaluation": binding}
        )
        session = NativeJudgeSession(
            ResearchModels(factory, recovery=recovery), recovery, manifest
        )
        yield session
    finally:
        try:
            if factory is not None:
                await factory.aclose()
        finally:
            try:
                if recovery is not None:
                    try:
                        budget = await store.budget(
                            recovery.lease.run_id, recovery.lease.user_id
                        )
                        (directory / "budget.json").write_text(
                            json.dumps(budget, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    finally:
                        await recovery.close()
            finally:
                await store.aclose()
