"""Coverage-bound quality contracts and deterministic admission policy."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Literal, Mapping, Sequence, cast

from pydantic import BaseModel, ConfigDict, Field, create_model

from open_deep_research.documents.contracts import SourceSelection, SourceMode

from open_deep_research.report.coverage import (
    CoverageSection,
    derive_coverage_sections,
    derive_coverage_units,
    is_scope_exclusion,
    source_directive_kind,
)

COVERAGE_CONTRACT_SCHEMA_VERSION = 2
QUALITY_RISK_POLICY_VERSION = "quality-risk-v1"
MAX_REQUIREMENTS_PER_RESEARCH_TASK = 3


def canonicalize_requirement_ids(values, contract):
    """Repair a model's hash typo only when its ordinal identifies one requirement."""
    allowed = contract.requirement_ids()
    by_ordinal = {}
    for value in allowed:
        by_ordinal.setdefault(value.rsplit("-", 1)[0], []).append(value)
    result = []
    for value in values:
        candidates = by_ordinal.get(value.rsplit("-", 1)[0], [])
        result.append(candidates[0] if value not in allowed and len(candidates) == 1 else value)
    return result


def validate_requirement_ids(values, contract, *, required):
    """Keep one delegation within its factual, atomic requirement scope."""
    normalized = list(dict.fromkeys(values))
    known = set(contract.requirement_ids()) if contract is not None else set(normalized)
    unknown = [value for value in normalized if value not in known]
    if unknown:
        raise ValueError("unknown_coverage_requirement_ids:" + ",".join(unknown))
    if contract is not None and contract.single_research_task:
        return list(contract.delegable_requirement_ids())
    delegable = set(contract.delegable_requirement_ids()) if contract is not None else known
    selected = [value for value in normalized if value in delegable]
    if len(selected) > MAX_REQUIREMENTS_PER_RESEARCH_TASK:
        raise ValueError(f"too_many_coverage_requirement_ids:{MAX_REQUIREMENTS_PER_RESEARCH_TASK}")
    dimensions = {item.dimension_id for item in contract.requirements
                  if item.requirement_id in selected and item.dimension_id} if contract is not None else set()
    if len(dimensions) > 1:
        raise ValueError("cross_dimension_requirement_ids:" + ",".join(sorted(dimensions)))
    if required and not selected and delegable:
        raise ValueError("non_delegable_requirement_ids_only" if normalized else "coverage_requirement_ids_required")
    return selected


def coverage_bound_input_schema(base_schema, contract):
    """Advertise factual IDs and task bounds without replacing runtime validation."""
    from copy import deepcopy

    ids = list(contract.delegable_requirement_ids()) if contract is not None else []
    if not ids:
        return base_schema
    field = deepcopy(base_schema.model_fields["requirement_ids"])
    field.json_schema_extra = {
        "items": {"type": "string", "enum": ids},
        "maxItems": len(ids) if contract.single_research_task else MAX_REQUIREMENTS_PER_RESEARCH_TASK,
    }
    return create_model(base_schema.__name__, __base__=base_schema, requirement_ids=(list[str], field))

RequirementKind = Literal["factual", "process", "deliverable"]

_SINGLE_RESEARCH_TASK_RE = re.compile(
    r"^(?:请你|请)?\s*(?:(?:采用|使用|只用|仅用|用|只创建|仅创建|只启动|仅启动)\s*"
    r"(?:一个|一项|1个|单一)(?:实质)?(?:子)?研究任务|单一研究任务)"
    r"(?:[。.!！\s]|完成|进行|$)|"
    r"^(?:please\s+)?(?:use|create|start)\s+(?:only\s+)?"
    r"(?:a\s+single|one|1)\s+(?:indivisible\s+)?research\s+task\b",
    re.IGNORECASE,
)

# Orchestration/interaction directives the engine itself satisfies; a research
# subtask can never prove them with web evidence ("至少并行两个研究员",
# "不需要澄清", citation-scope rules).
_PROCESS_REQUIREMENT_RE = re.compile(
    r"^(?:请\s*)?(?:不要|不得|禁止|不)(?:把|将).{0,32}(?:当作|视为).{0,16}(?:性能)?基准\s*$|"
    r"^(?:请\s*)?(?:不要|不得|禁止|不)(?:推测|编造|杜撰)(?:任何)?(?:未公开的?)?(?:性能数字|性能数据|性能指标)\s*$|"
    r"^(?:请\s*)?(?:仅|只)(?:直接)?(?:读取|访问|使用)(?:所选|指定|该|此)\s*(?:URL|网址|网页|页面|来源)\s*$|"
    r"^(?:不得|禁止|不要)(?:搜索|访问)(?:或(?:访问|搜索))?(?:任何)?(?:其他|额外|未指定)(?:网站|网页|页面|来源)\s*$|"
    r"(?:不可拆分|单一研究任务)|"
    r"(?:至少.{0,48}(?:并行.{0,16})?(?:Subagent|子智能体|研究员))|"
    r"(?:并行.{0,24}(?:委派|开展|执行|运行)?.{0,16}(?:两个|多个|多名|两名)?.{0,16}(?:Subagent|子智能体|研究员))|"
    r"(?:每个.{0,24}(?:Subagent|子智能体|研究员).{0,80}(?:读取|引用|来源))|"
    r"(?:(?:只|仅|严格).{0,480}(?:URL|网址|链接).{0,80}(?:证据|来源))|"
    r"(?:不得.{0,32}(?:引用|使用).{0,32}(?:其他|额外|未指定).{0,16}(?:URL|网址|链接|来源))|"
    r"(?:每项.{0,48}(?:引用|来源))|"
    r"(?:(?:必须|需要).{0,48}标(?:为|注为).{0,24}未证实)|"
    r"(?:不得.{0,80}(?:引用第三方|搜索候选摘要|当作证据))|"
    r"(?:基于|依据|使用|引用|参考).{0,16}至少.{0,8}[一二两三四五六七八九十\d]+.{0,16}(?:个|篇)?(?:不同的?)?(?:公开|可靠|权威|独立|官方)?(?:来源|数据源|证据源|文献)|"
    r"(?:based\s+on|use|cite|reference).{0,24}at\s+least\s+(?:two|three|four|five|\d+).{0,32}(?:sources?|references?)|"
    r"(?:不?需要澄清|无需澄清|不?需要确认|无需确认)|"
    r"(?:直接(?:执行|开始|研究|运行)|立即(?:执行|开始)|不必等待)|"
    r"(?:single\s+(?:indivisible\s+)?research\s+task)|"
    r"(?:must\s+(?:cite|label).{0,80})|"
    r"(?:do\s+not\s+(?:cite|use).{0,80})|"
    r"(?:no\s+clarification.{0,40})|"
    r"(?:proceed\s+directly.{0,40})",
    flags=re.IGNORECASE | re.DOTALL,
)

# Imperative team instructions are verified by the coordinator, not web evidence.
# Anchor these forms so questions *about* TeamCreate/CAS remain researchable.
_TEAM_PROCESS_REQUIREMENT_RE = re.compile(
    r"^(?:请\s*)?(?:团队执行要求[：:]\s*)?(?:"
    r"Lead\s*(?:显式\s*)?(?:调用\s*)?TeamCreate\b|"
    r"(?:调用|使用|通过)\s*(?:TeamCreate|SpawnTeammate|TaskGet|TaskUpdate|SendMessage)\b|"
    r"(?:创建|启动)?\s*[\w-]+\s*[（(](?:direct|plan_approval)[）)]|"
    r"(?:分别)?派发.{0,32}任务|blockedBy\b|不设置\s*owner\b|"
    r"让成员.{0,16}认领|成员(?:相互|之间)?.{0,16}发送.{0,16}消息|"
    r"\S+\s*(?:首版|第一版)计划.{0,24}Lead.{0,24}(?:驳回|审核)|"
    r"修订后.{0,24}(?:plan_response|批准)|"
    r"质量拒绝.{0,16}(?:不能|不得).{0,16}解锁|"
    r"(?:需要)?补证时\s*Lead\s*创建|(?:最后)?完成质量检查|"
    r"不(?:使用知识库|扩展架构推断)|每项充分取证|优先官方资料)",
    flags=re.IGNORECASE,
)

# Output-format obligations owned by the final report stage ("风险矩阵",
# "检查清单", "用中文输出"); evidence cannot prove a deliverable's existence.
_DELIVERABLE_REQUIREMENT_RE = re.compile(
    r"^(?:请\s*)?(?:包含|提供|给出|附上)(?:执行)?摘要\s*$|"
    r"^(?:请\s*)?(?:保留|提供|附上)(?:该|此|所选|指定)?(?:网页|页面|来源)的?(?:引用|链接)\s*$|"
    r"(?:最终.{0,32}(?:中文.{0,16})?(?:对照表|比较表|对比表|表格|简报|报告|输出|呈现))|"
    r"(?:(?:用|使用|以).{0,12}(?:中文|英文).{0,24}(?:输出|撰写|呈现|回答|报告))|"
    r"(?:执行摘要|executive\s+summary)|"
    r"(?:风险矩阵|risk\s+matrix)|"
    r"(?:(?:检查|核对|排查|验证)清单|(?:pre-?)?(?:launch|production|go-?live).{0,24}checklist)|"
    r"(?:(?:对照|比较)表|对比表格)|"
    r"(?:可点击.{0,8}(?:引用|链接)|clickable\s+(?:citation|link))|"
    r"(?:输出.{0,16}(?:表格|清单|矩阵|摘要))|"
    r"(?:报告(?:末尾|结尾|中).{0,24}(?:附|包含|提供))|"
    r"(?:(?:给出|提供|输出|撰写).{0,32}(?:中文|英文)?[^。;\\n]{0,16}(?:选型)?(?:建议|推荐|意见|结论))|"
    r"(?:(?:give|provide|offer|deliver).{0,24}(?:recommendation|advice|suggestion))|"
    r"(?:附上?.{0,16}(?:可核验|可验证|权威|官方)?的?(?:来源|参考|引用)?链接)|"
    r"(?:(?:attach|include|append).{0,24}verifiable.{0,16}(?:source\s+)?links?)|"
    r"(?:verifiable\s+(?:source\s+)?links?)|"
    # Length/brevity output constraints ("控制在简短报告内", "不超过一页"):
    # a research subtask cannot prove them with web evidence either.
    r"(?:控制在.{0,24}(?:报告|篇幅|长度|字数|[一二两三四五六七八九十百千万\d]+\s*(?:字|词|页|段|行)).{0,4}(?:以)?内)|"
    r"(?:(?:不超过|少于|最多|至多|限制在|限制为).{0,16}(?:字|词|页|段|行))|"
    r"(?:(?:简短|简洁|简明|精简|扼要|简练).{0,12}(?:报告|总结|摘要|输出|回答|说明|篇幅|版本))|"
    r"(?:篇幅.{0,12}(?:限制|要求|控制))|"
    r"(?:keep\s+(?:it\s+|the\s+)?(?:short|brief|concise))|"
    r"(?:(?:no\s+more\s+than|less\s+than|at\s+most|under)\s+\d+\s+(?:words?|pages?|paragraphs?|sentences?))|"
    r"(?:(?:short|brief|concise|one[-\s]page)\s+(?:report|summary|answer|overview|write-?up))",
    flags=re.IGNORECASE | re.DOTALL,
)

# Leading constraint labels that describe how the research/report must be
# produced, not a researchable dimension. After sentence-level splitting these
# trailing directives became their own items; classifying them factual let the
# Supervisor attach "来源要求/时间覆盖" to every task and the Judge scored them
# as uncovered dimensions (E2E af84fda9: 9/9 rejected on evidence_coverage).
_GLOBAL_CONSTRAINT_LABEL_RE = re.compile(
    r"^(?:来源要求|引用要求|时间覆盖|时间范围|报告结构|输出形式|篇幅要求|格式要求|"
    r"来源|结构|覆盖时间|时间要求)[：:]",
    flags=re.IGNORECASE,
)

_DIMENSION_CANDIDATE_RE = re.compile(
    r"^(?P<label>[^：:\n]{2,80})[：:]\s*(?P<body>.+)$",
    flags=re.DOTALL,
)
_COMPANY_ROSTER_RE = re.compile(
    r"^(?:深入)?(?:分析|研究)\s*(?P<companies>.+?)等企业(?:的|在)"
    r"(?P<attributes>.+)$",
    flags=re.DOTALL,
)
_ACTION_START_RE = re.compile(r"(?:给出|比较|对比|分析|评估|说明)")
_FINAL_ATOMIC_CONJUNCTION_RE = re.compile(r"\s*(?:以及|和|与)\s*(?=\S)")
_LEADING_ATOMIC_CONJUNCTION_RE = re.compile(r"^(?:以及|并且|同时|并|和|与)\s*")
_DIMENSION_LIST_INTRO_RE = re.compile(r"包括(?:但不限于)?[：:]?\s*")
_COMPARISON_AXIS_INTRO_RE = re.compile(
    r"(?:三地(?:的)?(?:以下维度)?[：:]?|以下维度[：:])"
)
_SUBITEM_MARKER_RE = re.compile(r"[\(（]\d{1,2}[\)）]\s*")


def classify_requirement_kind(text: str) -> RequirementKind:
    """Classify one requirement text as factual, process, or deliverable.

    Factual requirements need external evidence; process requirements are
    orchestration directives the engine satisfies itself; deliverable
    requirements are output-format obligations owned by the final report.
    """
    value = str(text or "")
    directive = source_directive_kind(value)
    if directive is not None:
        return cast(RequirementKind, directive)
    if is_scope_exclusion(value):
        return "process"
    if (_PROCESS_REQUIREMENT_RE.search(value)
            or _SINGLE_RESEARCH_TASK_RE.search(value.strip())
            or _TEAM_PROCESS_REQUIREMENT_RE.search(value.strip())):
        return "process"
    if _GLOBAL_CONSTRAINT_LABEL_RE.match(value.strip()):
        return "process"
    if _DELIVERABLE_REQUIREMENT_RE.search(value):
        return "deliverable"
    return "factual"


def is_delegable_requirement(
    requirement: CoverageRequirement | Mapping[str, Any],
) -> bool:
    """Return whether a research subtask can own this requirement.

    Only factual requirements are delegable. An explicit ``process`` /
    ``deliverable`` kind is authoritative; an explicit ``factual`` kind (or a
    payload compiled before kinds existed) still gets the pattern fallback so
    hand-built and legacy requirements classify the same way the compiler
    would have classified them.
    """
    if isinstance(requirement, CoverageRequirement):
        kind: RequirementKind | None = requirement.kind
        text = requirement.text
    else:
        raw_kind = requirement.get("kind")
        kind = None
        if isinstance(raw_kind, str) and raw_kind in (
            "factual",
            "process",
            "deliverable",
        ):
            kind = cast(RequirementKind, raw_kind)
        text = str(requirement.get("text", ""))
    if kind is not None and kind != "factual":
        return False
    return classify_requirement_kind(text) == "factual"


class AdmissionStatus(str, Enum):
    """Supervisor admission result for one Researcher handoff."""

    ACCEPTED = "accepted"
    ACCEPTED_WITH_CAVEATS = "accepted_with_caveats"
    REJECTED = "rejected"


class CoverageStatus(str, Enum):
    """Evidence coverage for one explicit user requirement."""

    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class CoverageRequirement(BaseModel):
    """One requirement copied from an original human message."""

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    text: str
    kind: RequirementKind = "factual"
    dimension_id: str | None = None
    source_message_index: int = Field(ge=0)
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=0)
    source_located: bool = True


class CoverageDimension(BaseModel):
    """One source-addressable parent dimension containing atomic requirements."""

    model_config = ConfigDict(frozen=True)

    dimension_id: str
    label: str
    text: str
    source_message_index: int = Field(ge=0)
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=0)
    source_located: bool = True
    requirement_ids: tuple[str, ...] = ()


class ResearchCoverageContract(BaseModel):
    """Immutable contract separating user requirements from model advice."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = COVERAGE_CONTRACT_SCHEMA_VERSION
    original_query_sha256: str
    requirements: tuple[CoverageRequirement, ...]
    dimensions: tuple[CoverageDimension, ...] = ()
    advisory_dimensions: tuple[str, ...] = ()
    single_research_task: bool = False
    source_selection: SourceSelection | None = None

    def requirement_ids(self) -> tuple[str, ...]:
        """Return stable requirement identifiers in source order."""
        return tuple(item.requirement_id for item in self.requirements)

    def delegable_requirement_ids(self) -> tuple[str, ...]:
        """Return IDs of factual requirements a research subtask may own."""
        return tuple(
            item.requirement_id
            for item in self.requirements
            if is_delegable_requirement(item)
        )

    def dimension_for_requirement(
        self,
        requirement_id: str,
    ) -> CoverageDimension | None:
        """Return the parent dimension for an atomic requirement, when present."""
        requirement = next(
            (
                item
                for item in self.requirements
                if item.requirement_id == requirement_id
            ),
            None,
        )
        if requirement is None or not requirement.dimension_id:
            return None
        return next(
            (
                dimension
                for dimension in self.dimensions
                if dimension.dimension_id == requirement.dimension_id
            ),
            None,
        )


def task_coverage_contract(contract, requirement_ids):
    """Narrow a Hybrid leaf only from user-authored source obligations.

    Model task prose cannot change this boundary. Mixed or unclassified owners
    retain the run contract, as does run-level completion without owned IDs.
    """
    if contract is None:
        return None
    contract = ResearchCoverageContract.model_validate(contract)
    selection = contract.source_selection
    if selection is None or selection.mode != SourceMode.HYBRID or not requirement_ids:
        return contract
    owned = set(requirement_ids) & set(contract.delegable_requirement_ids())
    modes = {}
    current = None
    message_index = None
    for requirement in sorted(contract.requirements, key=lambda r: (r.source_message_index, r.source_start)):
        if requirement.source_message_index != message_index:
            current, message_index = None, requirement.source_message_index
        text = coverage_requirement_display_text(contract, requirement)
        document = bool(re.search(r"(?:依据|基于|查阅|读取|使用).{0,12}(?:所选|指定|所提供|提供的).{0,12}(?:资料|文档|文件)", text))
        web = bool(re.search(r"(?:查阅|读取|访问|检索).{0,40}(?:网页|网站|官方发布说明|https?://)|(?:公开网络|网页证据)", text))
        explicit = "documents" if document and not web else "web" if web and not document else None
        # Only an exclusive source directive carries across clauses. A source
        # mentioned in one factual task cannot narrow an unrelated sibling.
        if requirement.kind == "process" and re.match(r"^(?:请)?(?:仅|只)", requirement.text) and explicit:
            current = explicit
        if requirement.requirement_id in owned:
            modes[requirement.requirement_id] = explicit or current
    if owned and set(modes) == owned and set(modes.values()) == {"documents"} and selection.document_ids:
        selected = SourceSelection.model_validate({"mode": "documents", "sources": [
            source.model_dump(mode="json") for source in selection.sources if source.type == "document"
        ]})
        return contract.model_copy(update={"source_selection": selected})
    return contract


class DimensionCoverageSummary(BaseModel):
    """Derived parent coverage; the atomic ledger remains authoritative."""

    model_config = ConfigDict(frozen=True)

    dimension_id: str
    label: str
    status: CoverageStatus
    requirement_ids: tuple[str, ...]
    missing_requirement_ids: tuple[str, ...] = ()


class RequirementCoverage(BaseModel):
    """Judge-proposed evidence mapping for one explicit requirement."""

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    status: CoverageStatus
    evidence_ids: tuple[str, ...] = ()
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class HandoffPolicyInput:
    """Inputs consumed by the deterministic v4 admission reducer."""

    requested_status: AdmissionStatus
    requirement_coverage: tuple[RequirementCoverage, ...]
    caveats: tuple[str, ...]
    missing_information: tuple[str, ...]
    unsupported_claims: tuple[str, ...]
    deterministic_checks_passed: bool
    scores: tuple[int, int, int, int]
    dimension_floor: int
    average_floor: float
    caveat_admission_enabled: bool
    high_risk: bool
    evaluator_failed_closed: bool = False
    additional_hard_rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HandoffPolicyResult:
    """Final admission result after applying non-model policy."""

    admission_status: AdmissionStatus
    accepted: bool
    caveats: tuple[str, ...]
    hard_rejection_reasons: tuple[str, ...]


class ResearchRiskProfile(BaseModel):
    """Persistable result of deterministic high-risk classification."""

    model_config = ConfigDict(frozen=True)

    policy_version: str = QUALITY_RISK_POLICY_VERSION
    level: str
    categories: tuple[str, ...] = ()
    matched_rule_ids: tuple[str, ...] = ()

    @property
    def high_risk(self) -> bool:
        """Return whether caveat admission must be disabled."""
        return self.level == "high"


_HIGH_RISK_RULES: dict[str, tuple[tuple[str, str], ...]] = {
    "medical": (
        ("medical.diagnosis", r"\bdiagnos(?:is|e)\b|诊断|确诊"),
        ("medical.treatment", r"\btreat(?:ment)?\b|治疗方案|用药"),
        ("medical.prescription", r"\bprescri(?:be|ption)\b|处方|剂量"),
        ("medical.emergency", r"\bemergency\b|急救|急诊"),
    ),
    "legal": (
        ("legal.advice", r"\blegal advice\b|法律意见|法律建议"),
        ("legal.litigation", r"\blitigation\b|诉讼|起诉"),
        ("legal.liability", r"\bliability\b|法律责任|侵权责任"),
        ("legal.contract", r"\bcontract interpretation\b|合同解释"),
    ),
    "finance": (
        ("finance.investment", r"\binvest(?:ment|ing)\b|投资建议|证券"),
        (
            "finance.trading",
            r"\b(?:trade|trading)\s+(?:advice|recommendation|strategy|signal)s?\b|"
            r"\b(?:buy|sell)\b.{0,48}\b(?:stocks?|securit(?:y|ies)|"
            r"crypto(?:currenc(?:y|ies))?|funds?|bonds?|shares?|options?|futures?)\b|"
            r"交易策略|交易建议|买入|卖出",
        ),
        ("finance.credit", r"\bcredit\b|\bloan\b|信贷|贷款"),
        ("finance.tax", r"\btax advice\b|税务建议"),
        ("finance.insurance", r"\binsurance advice\b|保险建议"),
    ),
}

_EXPLICIT_TIME_CONSTRAINT_PATTERNS = (
    re.compile(
        r"截至\s*(?:"
        r"\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?"
        r"|目前|当前|今日|现在"
        r")"
    ),
    re.compile(
        r"\bas of\s+(?:"
        r"(?:january|february|march|april|may|june|july|august|"
        r"september|october|november|december)"
        r"(?:\s+\d{1,2},?)?\s+\d{4}"
        r"|\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?"
        r"|today|now|the current date"
        r")\b",
        flags=re.IGNORECASE,
    ),
)


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or message.get("type") or "").lower()
    return str(getattr(message, "role", None) or getattr(message, "type", "")).lower()


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    get_text = getattr(message, "get_text_content", None)
    if get_text is not None:
        return get_text() or ""
    return str(getattr(message, "content", "") or "")


def _stable_requirement_id(
    message_index: int,
    text: str,
    ordinal: int,
) -> str:
    digest = hashlib.sha256(
        f"{message_index}:{ordinal}:{text}".encode()
    ).hexdigest()[:12]
    return f"COV-{ordinal:02d}-{digest}"


def _stable_dimension_id(
    message_index: int,
    text: str,
    ordinal: int,
) -> str:
    digest = hashlib.sha256(
        f"{message_index}:{ordinal}:{text}".encode()
    ).hexdigest()[:12]
    return f"DIM-{ordinal:02d}-{digest}"


@dataclass(frozen=True, slots=True)
class _SourceRequirement:
    """One selected requirement with an exact source projection."""

    message_index: int
    text: str
    kind: RequirementKind
    source_start: int
    source_end: int
    dimension_id: str | None = None

    @property
    def source_located(self) -> bool:
        return self.source_end > self.source_start


@dataclass(frozen=True, slots=True)
class _SourceDimension:
    """A parent dimension detected directly in the user message."""

    dimension_id: str
    label: str
    text: str
    message_index: int
    source_start: int
    source_end: int

    @property
    def source_located(self) -> bool:
        return self.source_end > self.source_start


@dataclass(frozen=True, slots=True)
class _SourceRequirementGroup:
    """Atomic candidates sharing one source clause or parent dimension."""

    items: tuple[_SourceRequirement, ...]
    fallback: _SourceRequirement
    dimension: _SourceDimension | None = None


def _split_final_atomic_conjunction(value: str) -> list[str]:
    """Split the final explicit list pair without matching inside a word."""
    matches = list(_FINAL_ATOMIC_CONJUNCTION_RE.finditer(value))
    for match in reversed(matches):
        if (
            match.start() > 0
            and value[match.start() - 1 : match.end() + 1] == "共和国"
        ):
            continue
        left = value[: match.start()].strip()
        right = value[match.end() :].strip()
        if all(len(re.sub(r"\W+", "", item)) >= 2 for item in (left, right)):
            return [left, right]
    return [value]


def _split_top_level_values(
    value: str,
    *,
    separators: frozenset[str],
) -> list[str]:
    """Split text on selected separators outside balanced grouping marks."""
    parts: list[str] = []
    start = 0
    opening = {"(": ")", "（": "）", "[": "]", "【": "】"}
    closing = frozenset(opening.values())
    closing_stack: list[str] = []
    for index, character in enumerate(value):
        expected_closing = opening.get(character)
        if expected_closing is not None:
            closing_stack.append(expected_closing)
        elif character in closing:
            if closing_stack and character == closing_stack[-1]:
                closing_stack.pop()
        elif not closing_stack and character in separators:
            part = value[start:index].strip()
            if part:
                parts.append(part)
            start = index + 1
    final = value[start:].strip()
    if final:
        parts.append(final)
    return parts


def _split_top_level_dimension_list(value: str) -> list[str]:
    """Split an explicit list while retaining punctuation inside parentheses."""
    parts = _split_top_level_values(
        value,
        separators=frozenset({"、", ",", "，", ";", "；"}),
    )
    if not parts:
        return []
    parts[-1:] = _split_final_atomic_conjunction(parts[-1])
    return [
        _LEADING_ATOMIC_CONJUNCTION_RE.sub("", item).strip(" ：:。. ")
        for item in parts
        if _LEADING_ATOMIC_CONJUNCTION_RE.sub("", item).strip(" ：:。. ")
    ]


def _split_top_level_dimension_clauses(value: str) -> list[str]:
    """Split strong clause boundaries without fragmenting nested lists."""
    return [
        item.strip(" ：:。. ")
        for item in _split_top_level_values(
            value,
            separators=frozenset({";", "；", "。", "!", "！", "?", "？"}),
        )
        if item.strip(" ：:。. ")
    ]


def _combine_regulatory_agencies(items: list[str], source: str) -> list[str]:
    """Keep a leading agency pair with the regulation it jointly qualifies."""
    if len(items) < 2:
        return items
    first, second = items[:2]
    if not (
        re.search(r"(?:部|委|局|署|EPA|DOE)$", first, flags=re.IGNORECASE)
        and re.search(r"法规|政策|管理办法|资金支持", second)
    ):
        return items
    start = source.find(first)
    second_start = source.find(second, start + len(first))
    if start < 0 or second_start < 0:
        return items
    combined = source[start : second_start + len(second)].strip()
    return [combined, *items[2:]]


def _company_dimension_items(label: str, body: str) -> list[str]:
    if "企业" not in label:
        return []
    match = _COMPANY_ROSTER_RE.match(body.strip())
    if match is None:
        return []
    return _split_top_level_dimension_list(match.group("companies"))


def _comparison_dimension_items(label: str, body: str) -> list[str]:
    if "对比" not in label and "比较" not in label:
        return []
    intro = _COMPARISON_AXIS_INTRO_RE.search(body)
    if intro is not None:
        tail = body[intro.end() :].strip()
    else:
        generic = re.match(r"^(?:必须系统)?比较.+?的(?P<tail>.+)$", body)
        if generic is None:
            return []
        tail = generic.group("tail").strip()
    clauses = _split_top_level_dimension_clauses(tail)
    items = (
        clauses
        if len(clauses) >= 2
        else _split_top_level_dimension_list(tail)
    )
    return [
        _SUBITEM_MARKER_RE.sub("", item).strip()
        for item in items
        if _SUBITEM_MARKER_RE.sub("", item).strip()
    ]


def _regional_dimension_items(label: str, body: str) -> list[str]:
    intro = _DIMENSION_LIST_INTRO_RE.search(body)
    if intro is None:
        if not re.search(r"政策|法规", label):
            return []
        tail = re.sub(r"^(?:研究|分析)\s*", "", body).strip()
        prefix = ""
    else:
        tail = body[intro.end() :].strip()
        prefix = body[: intro.start()]
    clauses = _split_top_level_dimension_clauses(tail)
    items = (
        clauses
        if len(clauses) >= 2
        else _combine_regulatory_agencies(
            _split_top_level_dimension_list(tail),
            tail,
        )
    )
    acronym = re.search(r"(?:EPA|DOE)", prefix)
    if acronym is not None and re.search(r"政策|资金支持", prefix[acronym.start() :]):
        focus = prefix[acronym.start() :].strip(" ，,；;：:。. ")
        if focus and focus not in items:
            items.insert(0, focus)
    return items


def _action_dimension_items(body: str) -> list[str]:
    matches = list(_ACTION_START_RE.finditer(body))
    if len(matches) < 2:
        return []
    items: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        item = body[match.start() : end].strip(" ，,；;：:。. ")
        item = re.sub(r"[，,；;]?\s*并$", "", item).strip()
        item = _LEADING_ATOMIC_CONJUNCTION_RE.sub("", item).strip()
        if item:
            items.append(item)
    return items


def _atomic_dimension_items(candidate: str) -> tuple[str, tuple[str, ...]] | None:
    """Return deterministic child fragments for one explicitly structured dimension."""
    match = _DIMENSION_CANDIDATE_RE.match(candidate.strip())
    if match is None:
        return None
    label = match.group("label").strip()
    body = match.group("body").strip()
    strategies = (
        _company_dimension_items(label, body),
        _comparison_dimension_items(label, body),
        _regional_dimension_items(label, body),
        _action_dimension_items(body),
    )
    for values in strategies:
        deduped = tuple(dict.fromkeys(item for item in values if item in candidate))
        if len(deduped) >= 2:
            return label, deduped
    return None


def _extract_explicit_time_constraints(
    content: str,
) -> tuple[tuple[str, int, int], ...]:
    """Return exact, source-addressable time constraints in source order."""
    matches: list[tuple[str, int, int]] = []
    occupied: set[tuple[int, int]] = set()
    for pattern in _EXPLICIT_TIME_CONSTRAINT_PATTERNS:
        for match in pattern.finditer(content):
            span = match.span()
            if span in occupied:
                continue
            occupied.add(span)
            matches.append((match.group(0), span[0], span[1]))
    return tuple(sorted(matches, key=lambda item: (item[1], item[2])))


def _locate_requirement_source(
    content: str,
    requirement: str,
    *,
    search_start: int = 0,
    search_end: int | None = None,
) -> tuple[str, int, int]:
    """Locate a normalized checklist item in its original source text."""
    bounded_end = len(content) if search_end is None else search_end
    direct_start = content.find(requirement, search_start, bounded_end)
    if direct_start >= 0:
        direct_end = direct_start + len(requirement)
        return content[direct_start:direct_end], direct_start, direct_end

    tokens = requirement.split()
    if tokens:
        whitespace_tolerant = re.compile(
            r"\s+".join(re.escape(token) for token in tokens),
        )
        match = whitespace_tolerant.search(content, search_start, bounded_end)
        if match is not None:
            start, end = match.span()
            return content[start:end], start, end

    # The checklist extractor already bounds semantic items to 500 characters.
    # If normalization removed markup or prefixes so thoroughly that no honest
    # source span can be recovered, retain that bounded item and mark the span
    # as unavailable instead of duplicating the entire user message.
    return requirement[:500], 0, 0


def _aggregate_factual_items(items: Sequence[str]) -> str:
    """Join overflow factual items into one bounded, delegable requirement."""
    joined = "、".join(item.strip(" ：:。. ") for item in items if item.strip())
    return re.sub(r"^(?:以及|并且|同时|并|和|与)\s*", "", joined)[:500]


def _source_requirement(
    message_index: int,
    content: str,
    text: str,
    kind: RequirementKind,
    *,
    dimension_id: str | None = None,
    search_start: int = 0,
    search_end: int | None = None,
) -> _SourceRequirement:
    source_item, start, end = _locate_requirement_source(
        content,
        text,
        search_start=search_start,
        search_end=search_end,
    )
    return _SourceRequirement(
        message_index=message_index,
        text=source_item,
        kind=kind,
        source_start=start,
        source_end=end,
        dimension_id=dimension_id,
    )


def _source_requirement_from_span(
    message_index: int,
    content: str,
    source_start: int,
    source_end: int,
    kind: RequirementKind,
    *,
    dimension_id: str | None = None,
) -> _SourceRequirement:
    """Build a requirement directly from a trusted source span."""
    start = max(0, source_start)
    end = min(len(content), source_end)
    while start < end and content[start].isspace():
        start += 1
    while end > start and content[end - 1].isspace():
        end -= 1
    return _SourceRequirement(
        message_index=message_index,
        text=content[start:end],
        kind=kind,
        source_start=start,
        source_end=end,
        dimension_id=dimension_id,
    )


def _grouping_marks_balanced(value: str) -> bool:
    """Return whether supported grouping marks are properly balanced."""
    opening = {"(": ")", "（": "）", "[": "]", "【": "】"}
    closing = frozenset(opening.values())
    closing_stack: list[str] = []
    for character in value:
        expected_closing = opening.get(character)
        if expected_closing is not None:
            closing_stack.append(expected_closing)
        elif character in closing:
            if not closing_stack or character != closing_stack.pop():
                return False
    return not closing_stack


def _structured_dimension_group(
    message_index: int,
    content: str,
    section: CoverageSection,
    dimension_ordinal: int,
) -> tuple[_SourceRequirementGroup, _SourceDimension]:
    """Compile one explicit bracketed dimension from exact source spans."""
    parent_text = content[section.source_start : section.source_end]
    body = content[section.body_start : section.body_end]
    dimension_id = _stable_dimension_id(
        message_index,
        parent_text,
        dimension_ordinal,
    )
    dimension = _SourceDimension(
        dimension_id=dimension_id,
        label=section.label,
        text=parent_text,
        message_index=message_index,
        source_start=section.source_start,
        source_end=section.source_end,
    )
    atomic = _atomic_dimension_items(f"{section.label}：{body}")
    atomic_texts = atomic[1] if atomic is not None else ()
    child_items: list[_SourceRequirement] = []
    search_start = section.body_start
    for item in atomic_texts:
        child = _source_requirement(
            message_index,
            content,
            item,
            "factual",
            dimension_id=dimension_id,
            search_start=search_start,
            search_end=section.body_end,
        )
        if (
            not child.source_located
            or not _grouping_marks_balanced(child.text)
            or "【维度" in child.text
        ):
            child_items = []
            break
        child_items.append(child)
        search_start = child.source_end

    if not child_items:
        fallback_start = (
            section.body_start
            if section.body_start < section.body_end
            else section.source_start
        )
        child_items.append(
            _source_requirement_from_span(
                message_index,
                content,
                fallback_start,
                section.body_end,
                "factual",
                dimension_id=dimension_id,
            )
        )
    fallback = _source_requirement_from_span(
        message_index,
        content,
        section.source_start,
        section.source_end,
        "factual",
        dimension_id=dimension_id,
    )
    return (
        _SourceRequirementGroup(
            items=tuple(child_items),
            fallback=fallback,
            dimension=dimension,
        ),
        dimension,
    )


def _legacy_source_requirement_groups(
    message_index: int,
    content: str,
    source_start: int,
    source_end: int,
    dimension_ordinal: int,
) -> tuple[
    list[_SourceRequirementGroup],
    list[_SourceDimension],
    int,
]:
    """Compile an unstructured source range through the legacy extractor."""
    groups: list[_SourceRequirementGroup] = []
    dimensions: list[_SourceDimension] = []
    for raw_group in derive_coverage_units(content[source_start:source_end]):
        if not raw_group:
            continue
        if len(raw_group) == 1:
            parent_text, parent_start, parent_end = _locate_requirement_source(
                content,
                raw_group[0],
                search_start=source_start,
                search_end=source_end,
            )
            kind = classify_requirement_kind(parent_text)
            atomic = (
                _atomic_dimension_items(parent_text)
                if kind == "factual" and parent_end > parent_start
                else None
            )
            if atomic is not None:
                label, atomic_items = atomic
                dimension_ordinal += 1
                dimension_id = _stable_dimension_id(
                    message_index,
                    parent_text,
                    dimension_ordinal,
                )
                dimension = _SourceDimension(
                    dimension_id=dimension_id,
                    label=label,
                    text=parent_text,
                    message_index=message_index,
                    source_start=parent_start,
                    source_end=parent_end,
                )
                child_items = tuple(
                    _source_requirement(
                        message_index,
                        content,
                        item,
                        "factual",
                        dimension_id=dimension_id,
                        search_start=parent_start,
                        search_end=parent_end,
                    )
                    for item in atomic_items
                )
                if len(child_items) >= 2 and all(
                    item.source_located for item in child_items
                ):
                    fallback = _source_requirement_from_span(
                        message_index,
                        content,
                        parent_start,
                        parent_end,
                        "factual",
                        dimension_id=dimension_id,
                    )
                    groups.append(
                        _SourceRequirementGroup(
                            items=child_items,
                            fallback=fallback,
                            dimension=dimension,
                        )
                    )
                    dimensions.append(dimension)
                    continue
        source_items = tuple(
            _source_requirement(
                message_index,
                content,
                item,
                classify_requirement_kind(item),
                search_start=source_start,
                search_end=source_end,
            )
            for item in raw_group
        )
        factual_texts = [
            item.text for item in source_items if item.kind == "factual"
        ]
        if len(source_items) > 1 and len(factual_texts) == len(source_items) and all(item.source_located for item in source_items):
            # Keep shared objects, versions and quantifiers with every child.
            start = min(item.source_start for item in source_items)
            end = max(item.source_end for item in source_items)
            parent = content[start:end]
            dimension_ordinal += 1
            dimension_id = _stable_dimension_id(message_index, parent, dimension_ordinal)
            dimension = _SourceDimension(dimension_id=dimension_id, label=parent[:80], text=parent,
                message_index=message_index, source_start=start, source_end=end)
            source_items = tuple(replace(item, dimension_id=dimension_id) for item in source_items)
            groups.append(_SourceRequirementGroup(items=source_items, dimension=dimension,
                fallback=_source_requirement_from_span(message_index, content, start, end, "factual", dimension_id=dimension_id)))
            dimensions.append(dimension)
            continue
        fallback_text = (
            _aggregate_factual_items(factual_texts)
            if factual_texts
            else source_items[0].text
        )
        fallback = _source_requirement(
            message_index,
            content,
            fallback_text,
            "factual" if factual_texts else source_items[0].kind,
            search_start=source_start,
            search_end=source_end,
        )
        groups.append(
            _SourceRequirementGroup(
                items=source_items,
                fallback=fallback,
            )
        )
    return groups, dimensions, dimension_ordinal


def _build_source_requirement_groups(
    human_payloads: Sequence[tuple[int, str]],
) -> tuple[list[_SourceRequirementGroup], list[_SourceDimension]]:
    """Project source clauses into atomic groups before applying the size cap."""
    groups: list[_SourceRequirementGroup] = []
    dimensions: list[_SourceDimension] = []
    dimension_ordinal = 0
    for message_index, content in human_payloads:
        sections = derive_coverage_sections(content)
        if not sections:
            legacy_groups, legacy_dimensions, dimension_ordinal = (
                _legacy_source_requirement_groups(
                    message_index,
                    content,
                    0,
                    len(content),
                    dimension_ordinal,
                )
            )
            groups.extend(legacy_groups)
            dimensions.extend(legacy_dimensions)
            continue

        if sections[0].source_start > 0:
            preamble = _source_requirement_from_span(
                message_index,
                content,
                0,
                sections[0].source_start,
                classify_requirement_kind(
                    content[: sections[0].source_start]
                ),
            )
            if preamble.text:
                groups.append(
                    _SourceRequirementGroup(
                        items=(preamble,),
                        fallback=preamble,
                    )
                )

        for section in sections:
            if section.kind == "dimension":
                dimension_ordinal += 1
                group, dimension = _structured_dimension_group(
                    message_index,
                    content,
                    section,
                    dimension_ordinal,
                )
                groups.append(group)
                dimensions.append(dimension)
            else:
                requirement = _source_requirement_from_span(
                    message_index,
                    content,
                    section.source_start,
                    section.source_end,
                    cast(RequirementKind, section.kind),
                )
                groups.append(
                    _SourceRequirementGroup(
                        items=(requirement,),
                        fallback=requirement,
                    )
                )
    return groups, dimensions


def build_research_coverage_contract(
    messages: Sequence[Any],
    *,
    advisory_dimensions: Iterable[str] = (),
    max_requirements: int = 48,
) -> ResearchCoverageContract:
    """Build an immutable contract only from original human messages.

    Explicitly structured factual dimensions become parent records whose atomic
    children are the only delegable requirements. Process/deliverable
    constraints are always admitted. Under cap pressure each source group falls
    back to one complete parent/clause instead of silently dropping its tail.
    """
    human_payloads = [
        (index, _message_content(message))
        for index, message in enumerate(messages)
        if _message_role(message) in {"human", "user"}
        and _message_content(message).strip()
    ]
    query_text = "\n\n".join(text for _, text in human_payloads)
    single_research_task = any(
        _SINGLE_RESEARCH_TASK_RE.search(clause.strip())
        for _, text in human_payloads
        for clause in re.split(r"[。！？.!?\n;；，,]", text)
    )

    source_groups, source_dimensions = _build_source_requirement_groups(
        human_payloads
    )
    selected: list[_SourceRequirement] = []
    seen: set[tuple[str, str]] = set()

    def admit(item: _SourceRequirement) -> bool:
        normalized = re.sub(r"\W+", "", item.text).lower()
        dedupe_scope = item.dimension_id or "global"
        key = (dedupe_scope, normalized)
        if not normalized or key in seen:
            return False
        seen.add(key)
        selected.append(item)
        return True

    # Pass 1 — explicit time constraints and constraint-kind checklist items
    # are always admitted regardless of the factual cap.
    for message_index, content in human_payloads:
        for constraint_text, start, end in _extract_explicit_time_constraints(
            content
        ):
            admit(
                _SourceRequirement(
                    message_index=message_index,
                    text=constraint_text,
                    kind=classify_requirement_kind(constraint_text),
                    source_start=start,
                    source_end=end,
                )
            )
    for group in source_groups:
        for source_item in group.items:
            if source_item.kind != "factual":
                admit(source_item)

    # Pass 2 — reserve at least one slot per factual source group, then retain
    # atomic children whenever the remaining budget can hold the whole group.
    factual_groups = [
        (
            group,
            tuple(
                source_item
                for source_item in group.items
                if source_item.kind == "factual"
            ),
        )
        for group in source_groups
        if any(source_item.kind == "factual" for source_item in group.items)
    ]
    factual_already_admitted = sum(
        source_item.kind == "factual" for source_item in selected
    )
    factual_budget = max(
        max(1, max_requirements - factual_already_admitted),
        len(factual_groups),
    )
    for index, (group, factual_items) in enumerate(factual_groups):
        groups_after = len(factual_groups) - index - 1
        expansion_capacity = max(1, factual_budget - groups_after)
        chosen = (
            factual_items
            if len(factual_items) <= expansion_capacity
            else (group.fallback,)
        )
        admitted_count = sum(
            1 for source_item in chosen if admit(source_item)
        )
        factual_budget -= admitted_count

    if not selected and query_text.strip():
        fallback = query_text.strip()[:240]
        selected.append(
            _source_requirement(
                human_payloads[0][0],
                human_payloads[0][1],
                fallback,
                classify_requirement_kind(fallback),
            )
        )

    requirements: list[CoverageRequirement] = []
    dimension_requirement_ids: dict[str, list[str]] = {
        dimension.dimension_id: [] for dimension in source_dimensions
    }
    for ordinal, source_item in enumerate(selected, start=1):
        requirement_id = _stable_requirement_id(
            source_item.message_index,
            source_item.text,
            ordinal,
        )
        requirements.append(
            CoverageRequirement(
                requirement_id=requirement_id,
                text=source_item.text,
                kind=source_item.kind,
                dimension_id=source_item.dimension_id,
                source_message_index=source_item.message_index,
                source_start=source_item.source_start,
                source_end=source_item.source_end,
                source_located=source_item.source_located,
            )
        )
        if source_item.dimension_id:
            dimension_requirement_ids[source_item.dimension_id].append(
                requirement_id
            )
    dimensions = tuple(
        CoverageDimension(
            dimension_id=dimension.dimension_id,
            label=dimension.label,
            text=dimension.text,
            source_message_index=dimension.message_index,
            source_start=dimension.source_start,
            source_end=dimension.source_end,
            source_located=dimension.source_located,
            requirement_ids=tuple(
                dimension_requirement_ids.get(dimension.dimension_id, ())
            ),
        )
        for dimension in source_dimensions
        if dimension_requirement_ids.get(dimension.dimension_id)
    )
    advisories = tuple(
        dict.fromkeys(
            str(item).strip()[:500]
            for item in advisory_dimensions
            if str(item).strip()
        )
    )
    return ResearchCoverageContract(
        original_query_sha256=hashlib.sha256(
            query_text.encode("utf-8")
        ).hexdigest(),
        requirements=tuple(requirements),
        dimensions=dimensions,
        advisory_dimensions=advisories,
        single_research_task=single_research_task,
    )


def coverage_requirement_display_text(
    contract: ResearchCoverageContract,
    requirement: CoverageRequirement,
) -> str:
    """Render an atomic requirement with its parent dimension context."""
    dimension_id = getattr(requirement, "dimension_id", None)
    if not dimension_id:
        return requirement.text
    dimension = next(
        (
            item
            for item in getattr(contract, "dimensions", ())
            if item.dimension_id == dimension_id
        ),
        None,
    )
    if dimension is None or requirement.text.startswith(
        (f"{dimension.label}：", f"{dimension.label}:")
    ):
        return requirement.text
    return f"{dimension.label}：{requirement.text}"


def aggregate_dimension_coverage(
    contract: ResearchCoverageContract,
    ledger: Mapping[str, Mapping[str, Any]],
) -> tuple[DimensionCoverageSummary, ...]:
    """Derive parent status from the authoritative atomic coverage ledger."""
    summaries: list[DimensionCoverageSummary] = []
    for dimension in contract.dimensions:
        statuses: list[str] = []
        for requirement_id in dimension.requirement_ids:
            entry = ledger.get(requirement_id, {})
            statuses.append(
                str(entry.get("status", "unsupported"))
                if isinstance(entry, Mapping)
                else CoverageStatus.UNSUPPORTED.value
            )
        missing = tuple(
            requirement_id
            for requirement_id, status in zip(
                dimension.requirement_ids,
                statuses,
            )
            if status != CoverageStatus.SUPPORTED.value
        )
        if statuses and all(
            status == CoverageStatus.SUPPORTED.value for status in statuses
        ):
            status = CoverageStatus.SUPPORTED
        elif any(
            value
            in {
                CoverageStatus.SUPPORTED.value,
                CoverageStatus.PARTIAL.value,
            }
            for value in statuses
        ):
            status = CoverageStatus.PARTIAL
        else:
            status = CoverageStatus.UNSUPPORTED
        summaries.append(
            DimensionCoverageSummary(
                dimension_id=dimension.dimension_id,
                label=dimension.label,
                status=status,
                requirement_ids=dimension.requirement_ids,
                missing_requirement_ids=missing,
            )
        )
    return tuple(summaries)


def classify_research_risk(
    text: str,
    *,
    mode: str = "auto",
    skills: Iterable[str] = (),
) -> ResearchRiskProfile:
    """Classify high-risk research using versioned deterministic rules."""
    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in {"auto", "high", "standard"}:
        raise ValueError(f"unsupported_quality_risk_mode:{mode}")
    if normalized_mode == "high":
        return ResearchRiskProfile(
            level="high",
            matched_rule_ids=("config.force_high",),
        )
    if normalized_mode == "standard":
        return ResearchRiskProfile(
            level="standard",
            matched_rule_ids=("config.force_standard",),
        )

    categories: set[str] = {
        skill
        for skill in (str(item).strip().lower() for item in skills)
        if skill in _HIGH_RISK_RULES
    }
    matched_rules: set[str] = {
        f"skill.{category}" for category in categories
    }
    for category, rules in _HIGH_RISK_RULES.items():
        for rule_id, pattern in rules:
            if re.search(pattern, text, flags=re.IGNORECASE):
                categories.add(category)
                matched_rules.add(rule_id)
    return ResearchRiskProfile(
        level="high" if categories else "standard",
        categories=tuple(sorted(categories)),
        matched_rule_ids=tuple(sorted(matched_rules)),
    )


def resolve_handoff_admission(
    policy_input: HandoffPolicyInput,
    *,
    owned_requirement_ids: Iterable[str],
    hard_requirement_ids: Iterable[str] | None = None,
) -> HandoffPolicyResult:
    """Apply hard safety and coverage rules after the model assessment.

    ``hard_requirement_ids`` narrows the per-task hard coverage gate to
    requirements this task owns exclusively; co-owned requirements are
    aggregated by the run-level coverage ledger instead of being enforced
    against every broader task (E2E 01517727: the SQLite task was rejected
    solely for a PostgreSQL requirement owned by a sibling task).
    """
    owned = tuple(dict.fromkeys(str(item) for item in owned_requirement_ids))
    hard = tuple(
        dict.fromkeys(
            str(item)
            for item in (
                hard_requirement_ids
                if hard_requirement_ids is not None
                else owned
            )
        )
    )
    coverage_by_id = {
        item.requirement_id: item
        for item in policy_input.requirement_coverage
    }
    hard_reasons: list[str] = []
    hard_reasons.extend(policy_input.additional_hard_rejection_reasons)
    if not owned:
        hard_reasons.append("owned_requirements_missing")
    if not policy_input.deterministic_checks_passed:
        hard_reasons.append("deterministic_checks_failed")
    if policy_input.evaluator_failed_closed:
        hard_reasons.append("quality_evaluator_failed_closed")
    if policy_input.unsupported_claims:
        hard_reasons.append("unsupported_claims")
    if min(policy_input.scores) < policy_input.dimension_floor:
        hard_reasons.append("score_below_dimension_floor")
    if (
        sum(policy_input.scores) / len(policy_input.scores)
        < policy_input.average_floor
    ):
        hard_reasons.append("score_below_average_floor")
    for requirement_id in hard:
        coverage = coverage_by_id.get(requirement_id)
        if coverage is None or coverage.status is not CoverageStatus.SUPPORTED:
            hard_reasons.append(
                f"required_coverage_missing:{requirement_id}"
            )

    caveats = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in (
                *policy_input.caveats,
                *policy_input.missing_information,
            )
            if str(item).strip()
        )
    )
    if hard_reasons:
        return HandoffPolicyResult(
            admission_status=AdmissionStatus.REJECTED,
            accepted=False,
            caveats=caveats,
            hard_rejection_reasons=tuple(dict.fromkeys(hard_reasons)),
        )
    if caveats:
        if (
            not policy_input.caveat_admission_enabled
            or policy_input.high_risk
        ):
            reason = (
                "high_risk_caveats_disallowed"
                if policy_input.high_risk
                else "caveat_admission_disabled"
            )
            return HandoffPolicyResult(
                admission_status=AdmissionStatus.REJECTED,
                accepted=False,
                caveats=caveats,
                hard_rejection_reasons=(reason,),
            )
        return HandoffPolicyResult(
            admission_status=AdmissionStatus.ACCEPTED_WITH_CAVEATS,
            accepted=True,
            caveats=caveats,
            hard_rejection_reasons=(),
        )
    return HandoffPolicyResult(
        admission_status=AdmissionStatus.ACCEPTED,
        accepted=True,
        caveats=(),
        hard_rejection_reasons=(),
    )


def merge_coverage_ledger(
    ledger: dict[str, dict[str, Any]],
    *,
    task_id: str,
    assessment: Any,
    owned_requirement_ids: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Merge an admitted assessment into a reducer-safe coverage ledger."""
    merged = {
        str(key): {
            "status": str(value.get("status", CoverageStatus.UNSUPPORTED.value)),
            "evidence_ids": list(value.get("evidence_ids", [])),
            "task_ids": list(value.get("task_ids", [])),
            "caveats": list(value.get("caveats", [])),
        }
        for key, value in ledger.items()
        if isinstance(value, dict)
    }
    admission_status = str(
        getattr(
            getattr(assessment, "admission_status", ""),
            "value",
            getattr(assessment, "admission_status", ""),
        )
    )
    if admission_status == AdmissionStatus.REJECTED.value:
        return merged
    assessment_caveats = [
        str(item) for item in getattr(assessment, "caveats", [])
    ]
    statuses = {
        CoverageStatus.UNSUPPORTED.value: 0,
        CoverageStatus.PARTIAL.value: 1,
        CoverageStatus.SUPPORTED.value: 2,
    }
    mapped_requirement_ids: set[str] = set()
    for coverage in getattr(assessment, "requirement_coverage", []):
        requirement_id = str(coverage.requirement_id)
        mapped_requirement_ids.add(requirement_id)
        current = merged.setdefault(
            requirement_id,
            {
                "status": CoverageStatus.UNSUPPORTED.value,
                "evidence_ids": [],
                "task_ids": [],
                "caveats": [],
            },
        )
        new_status = coverage.status.value
        if statuses[new_status] >= statuses.get(str(current["status"]), 0):
            current["status"] = new_status
        current["evidence_ids"] = list(
            dict.fromkeys(
                [
                    *current["evidence_ids"],
                    *(str(item) for item in coverage.evidence_ids),
                ]
            )
        )
        current["task_ids"] = list(
            dict.fromkeys([*current["task_ids"], task_id])
        )
        current["caveats"] = list(
            dict.fromkeys([*current["caveats"], *assessment_caveats])
        )
    for requirement_id in dict.fromkeys(
        str(item) for item in owned_requirement_ids if str(item)
    ):
        if requirement_id in mapped_requirement_ids:
            continue
        current = merged.setdefault(
            requirement_id,
            {
                "status": CoverageStatus.UNSUPPORTED.value,
                "evidence_ids": [],
                "task_ids": [],
                "caveats": [],
            },
        )
        if statuses.get(str(current["status"]), 0) < statuses[
            CoverageStatus.PARTIAL.value
        ]:
            current["status"] = CoverageStatus.PARTIAL.value
        current["task_ids"] = list(
            dict.fromkeys([*current["task_ids"], task_id])
        )
        current["caveats"] = list(
            dict.fromkeys([
                *current["caveats"],
                *assessment_caveats,
                "coverage_mapping_missing",
            ])
        )
    return merged
