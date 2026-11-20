"""Public inputs and immutable assessment contracts for continuous intelligence."""

from datetime import datetime, timedelta, UTC
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Cadence = Literal["manual", "hourly", "daily", "weekly"]
INTERVALS = {"manual": None, "hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(weeks=1)}
STRICT_QUALITY = {"quality_evaluation_enabled": True, "report_review_enabled": True,
                  "quality_evaluation_fail_open": False, "report_review_fail_open": False}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceInput(Input):
    kind: Literal["web", "knowledge"] = "web"
    url: str | None = Field(default=None, max_length=2048)
    document_id: UUID | None = None
    label: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def target(self):
        if self.kind == "web":
            from market_scout.security.network import validate_http_url_syntax
            from market_scout.documents.contracts import source_url_identity
            if not self.url or self.document_id:
                raise ValueError("网页来源必须提供 URL，不能同时指定文档")
            validate_http_url_syntax(self.url)
            self.url = source_url_identity(self.url)
        elif not self.document_id or self.url:
            raise ValueError("知识来源必须指定已发布文档")
        return self


class PlanBudget(Input):
    run_micro_usd: int = Field(gt=0)
    month_micro_usd: int = Field(gt=0)
    assessment_micro_usd: int = Field(default=100_000, gt=0)

    @model_validator(mode="after")
    def limits(self):
        if max(self.run_micro_usd, self.assessment_micro_usd) > self.month_micro_usd:
            raise ValueError("单次额度不能超过计划月度额度")
        return self


class PlanCreate(Input):
    workspace_id: UUID
    knowledge_base_id: UUID
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=12000)
    dimensions: list[str] = Field(default_factory=list, max_length=30)
    sources: list[SourceInput] = Field(min_length=1, max_length=100)
    budget: PlanBudget
    cadence: Cadence = "daily"
    timezone: str = "Asia/Shanghai"
    mode: Literal["observe", "auto"] = "observe"
    profile_revision_id: str | None = None
    configurable: dict = Field(default_factory=dict)
    reviewer_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        ZoneInfo(value)
        return value

    @field_validator("configurable")
    @classmethod
    def quality_required(cls, value):
        if value.get("enable_memory") is True:
            raise ValueError("持续计划仅使用已授权来源与计划历史，不读取个人研究记忆")
        if value.get("quality_evaluation_min_sources") in (0, "0"):
            raise ValueError("持续研究必须保留最低来源要求")
        for key, required in STRICT_QUALITY.items():
            if key in value and value[key] is not required:
                raise ValueError("持续研究必须启用全部质量门禁且关闭 fail-open：" + key)
        return {**value, **STRICT_QUALITY, "enable_memory": False}


class VersionedCommand(Input):
    expected_version: int = Field(ge=1)
    command_id: str = Field(min_length=1, max_length=160)


class PlanUpdate(VersionedCommand):
    plan: PlanCreate


class Activate(VersionedCommand):
    mode: Literal["observe", "auto"]
    expires_at: datetime

    @field_validator("expires_at", mode="before")
    @classmethod
    def displayed_expiration(cls, value):
        if isinstance(value, str) and len(value) == 19 and value[10] == " ":
            from market_scout.research_time import parse_business_time
            return parse_business_time(value)
        return value

    @field_validator("expires_at")
    @classmethod
    def expiration(cls, value):
        if value.tzinfo is None or value <= datetime.now(UTC):
            raise ValueError("授权到期时间必须包含时区且晚于当前时间")
        return value


class ItemAction(VersionedCommand):
    action: Literal["start", "feedback", "cancel", "retry", "dismiss", "supplement"]
    reason: str = Field(default="", max_length=12000)

    @model_validator(mode="after")
    def explanation(self):
        if self.action in {"feedback", "dismiss", "supplement"} and not self.reason.strip():
            raise ValueError("反馈或忽略必须说明理由")
        return self


class ItemReview(VersionedCommand):
    action: Literal["prepare_sources", "prepare_knowledge", "complete", "reject"]
    reason: str = Field(default="", max_length=12000)
    published_fact_ids: list[UUID] = Field(default_factory=list, max_length=100)
    published_wiki_revision_ids: list[UUID] = Field(default_factory=list, max_length=100)


class ImpactAssessment(Input):
    decision: Literal["ignore", "research", "needs_review"]
    reason: str = Field(min_length=1, max_length=4000)
    question: str = Field(default="", max_length=8000)
    affected_fact_ids: list[str] = Field(default_factory=list, max_length=100)
    affected_wiki_revision_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_snapshot_ids: list[str] = Field(default_factory=list, max_length=200)
    severity: Literal["low", "normal", "high"] = "normal"
    uncertainty: str = Field(default="", max_length=2000)


class FeishuPreference(Input):
    enabled: bool


def next_check(cadence: Cadence, at=None):
    interval = INTERVALS[cadence]
    return (at or datetime.now(UTC)) + interval if interval else None


def retry_after_seconds(value, at=None):
    """Respect both HTTP delay-seconds and HTTP-date forms."""
    from email.utils import parsedate_to_datetime
    import math
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - (at or datetime.now(UTC))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 60
    return max(1, math.ceil(seconds)) if math.isfinite(seconds) else 60


class IntelligenceError(ValueError):
    def __init__(self, code, status=409, *, current_version=None):
        self.code, self.status, self.current_version = code, status, current_version
        super().__init__(code)


class DeferredJob(Exception):
    """A paused effect remains queued; deferral is not a failed execution."""
