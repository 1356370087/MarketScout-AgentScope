"""可发布配置与控制面输入契约。"""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldPolicy(StrictModel):
    editable: bool = False
    minimum: float | None = None
    maximum: float | None = None
    allowed: list[Any] | None = None

    @model_validator(mode="after")
    def bounds(self):
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("配置下限不能大于上限")
        return self


class QuotaLimits(StrictModel):
    monthly_cost_micro_usd: int | None = Field(default=None, ge=0)
    max_run_cost_micro_usd: int | None = Field(default=None, ge=1)
    max_concurrent_runs_per_user: int | None = Field(default=None, ge=1)
    api_run_create_per_minute: int | None = Field(default=None, ge=1)
    max_documents_per_user: int | None = Field(default=None, ge=1)
    max_bytes_per_user: int | None = Field(default=None, ge=1)


class ConfigurationDocument(StrictModel):
    schema_version: Literal[1] = 1
    runtime: dict[str, Any] = Field(default_factory=dict)
    services: dict[str, Any] = Field(default_factory=dict)
    credential_refs: dict[str, str] = Field(default_factory=dict)
    litellm: dict[str, Any] = Field(default_factory=dict)
    sandbox: dict[str, Any] | None = None
    memory: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, FieldPolicy] = Field(default_factory=dict)
    quotas: QuotaLimits = Field(default_factory=QuotaLimits)
    allowed_providers: list[str] = Field(default_factory=lambda: ["openai", "anthropic", "deepseek", "gemini", "zhipu", "dashscope", "tavily", "brave", "bing"])
    allowed_model_endpoints: list[str] = Field(default_factory=list)


class DraftRequest(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    scope: Literal["global", "personal"] = "personal"
    profile_id: str | None = None
    base_revision: str | None = None
    document: ConfigurationDocument = Field(default_factory=ConfigurationDocument)


class UpdateDraft(StrictModel):
    expected_version: int = Field(ge=1)
    document: ConfigurationDocument


class PublishRequest(StrictModel):
    expected_version: int = Field(ge=1)
    expected_platform_revision: str | None = None
    reason: str = Field(min_length=1, max_length=1000)


class PreviewRequest(StrictModel):
    profile_revision_id: str | None = None
    expected_platform_revision: str | None = None
    configurable: dict[str, Any] = Field(default_factory=dict)


class PreferencesRequest(StrictModel):
    profile_revision_id: str | None = None
    configurable: dict[str, Any] = Field(default_factory=dict)
    publication_theme: dict[str, Any] = Field(default_factory=dict)


class CredentialRequest(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=64)
    scope: Literal["global", "personal"] = "personal"
    secret: str = Field(min_length=1, max_length=20000, repr=False)


class CredentialReplacement(StrictModel):
    secret: str = Field(min_length=1, max_length=20000, repr=False)
    expected_version: int = Field(ge=1)


class ImportRequest(StrictModel):
    name: str = Field(default="导入配置", min_length=1, max_length=160)
    scope: Literal["global", "personal"] = "personal"
    format: Literal["env", "yaml", "toml", "json", "server"]
    content: str = Field(default="", max_length=2000000, repr=False)


class BootstrapRequest(StrictModel):
    token: str = Field(min_length=1, max_length=512, repr=False)
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=200, repr=False)
    display_name: str = Field(default="管理员", max_length=160)


class MaintenanceRequest(StrictModel):
    action: Literal["cancel_run", "retry_document", "reindex_document", "retry_publication", "knowledge_maintenance"]
    resource_id: str
    reason: str = Field(min_length=1, max_length=1000)
