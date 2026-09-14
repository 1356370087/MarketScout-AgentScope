"""Public contracts for local documents and run source selection."""

from __future__ import annotations

import ipaddress
import re
from enum import Enum
from typing import Annotated, Literal, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourceMode(str, Enum):
    """Evidence channels available to a research run."""

    WEB = "web"
    DOCUMENTS = "documents"
    HYBRID = "hybrid"
    SPECIFIC = "specific"


_TRACKING_QUERY_PARAMS = frozenset(
    {"gclid", "fbclid", "dclid", "msclkid", "mc_cid", "mc_eid"}
)


def normalize_source_url(
    value: str,
    *,
    reject_private: bool = True,
    strip_trailing_slash: bool = True,
) -> str:
    """Normalize a user/provider HTTP URL to one comparable identity."""
    try:
        parsed = urlsplit(str(value).strip())
    except ValueError as exc:
        raise ValueError("source_url_must_be_http") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source_url_must_be_http")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source_url_credentials_not_allowed")
    raw_host = parsed.hostname.rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source_url_must_be_http") from exc
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        address = None
    if address is not None:
        host = raw_host.lower()
    else:
        try:
            host = raw_host.encode("idna").decode("ascii").rstrip(".").lower()
        except UnicodeError as exc:
            raise ValueError("source_url_must_be_http") from exc
    if reject_private and address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    ):
        raise ValueError("source_url_private_host_not_allowed")
    if ":" in host:
        netloc = f"[{host}]"
    else:
        netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    params = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_PARAMS
    ]
    query = urlencode(sorted(params), doseq=True)
    path = parsed.path or "/"
    if strip_trailing_slash and path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def source_url_identity(value: str) -> str:
    """Return a comparison identity, or an empty string for an invalid URL."""
    try:
        return normalize_source_url(
            value,
            reject_private=False,
            strip_trailing_slash=True,
        )
    except ValueError:
        return ""


class DocumentSourceRef(BaseModel):
    """One immutable local document selected for a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["document"]
    id: str = Field(min_length=1, max_length=128)


class KnowledgeBaseSourceRef(BaseModel):
    """One knowledge base expanded into its published documents at run start."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["knowledge_base"]
    id: str = Field(min_length=1, max_length=128)


class KnowledgeCollectionSourceRef(BaseModel):
    """One collection expanded into its published documents at run start."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["collection"]
    id: str = Field(min_length=1, max_length=128)


class URLSourceRef(BaseModel):
    """One exact HTTP source selected for a run.

    DNS and connected-peer checks remain deferred to the async network gate so
    hostname rebinding is checked at the point of egress.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["url"]
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        """Normalize one public HTTP URL and reject literal private targets."""
        return normalize_source_url(
            value,
            reject_private=True,
            strip_trailing_slash=True,
        )


class DomainSourceRef(BaseModel):
    """One DNS domain selected as a hard source boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["domain"]
    domain: str = Field(min_length=1, max_length=253)

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        """Normalize an internationalized domain into lower-case ASCII."""
        domain = value.strip().lower().rstrip(".")
        if "://" in domain:
            domain = (urlsplit(domain).hostname or "").lower()
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("invalid_source_domain") from exc
        if not re.fullmatch(
            r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
            domain,
        ):
            raise ValueError("invalid_source_domain")
        return domain


SourceRef = Annotated[
    Union[
        DocumentSourceRef,
        KnowledgeBaseSourceRef,
        KnowledgeCollectionSourceRef,
        URLSourceRef,
        DomainSourceRef,
    ],
    Field(discriminator="type"),
]


class SourceSelection(BaseModel):
    """Validated, extensible source selection attached to a run."""

    model_config = ConfigDict(extra="forbid")

    mode: SourceMode = SourceMode.WEB
    sources: list[SourceRef] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_mode_sources(self) -> SourceSelection:
        """Enforce source combinations and reject duplicate references."""
        material_count = sum(
            item.type in {"document", "knowledge_base", "collection"}
            for item in self.sources
        )
        non_material_count = len(self.sources) - material_count
        if self.mode is SourceMode.WEB and self.sources:
            raise ValueError("web_mode_does_not_accept_specific_sources")
        if self.mode in {SourceMode.DOCUMENTS, SourceMode.HYBRID}:
            if material_count == 0:
                raise ValueError("document_source_required")
            if non_material_count:
                raise ValueError("documents_and_hybrid_accept_document_sources_only")
        if self.mode is SourceMode.SPECIFIC and not self.sources:
            raise ValueError("specific_source_required")
        identities = [
            (
                item.type,
                getattr(item, "id", None)
                or getattr(item, "url", None)
                or getattr(item, "domain", None),
            )
            for item in self.sources
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate_source_reference")
        return self

    @property
    def document_ids(self) -> list[str]:
        """Return selected local document identifiers in request order."""
        return [item.id for item in self.sources if isinstance(item, DocumentSourceRef)]

    @property
    def urls(self) -> list[str]:
        """Return exact selected URLs in request order."""
        return [item.url for item in self.sources if isinstance(item, URLSourceRef)]

    @property
    def domains(self) -> list[str]:
        """Return selected domains in request order."""
        return [
            item.domain for item in self.sources if isinstance(item, DomainSourceRef)
        ]

    @property
    def web_enabled(self) -> bool:
        """Return whether the run may use governed Web evidence tools."""
        return self.mode in {SourceMode.WEB, SourceMode.HYBRID} or bool(
            self.urls or self.domains
        )

    @property
    def knowledge_base_ids(self) -> list[str]:
        """Return selected knowledge-base identifiers in request order."""
        return [
            item.id for item in self.sources
            if isinstance(item, KnowledgeBaseSourceRef)
        ]

    @property
    def collection_ids(self) -> list[str]:
        """Return selected collection identifiers in request order."""
        return [
            item.id for item in self.sources
            if isinstance(item, KnowledgeCollectionSourceRef)
        ]

    @property
    def documents_enabled(self) -> bool:
        """Return whether the run may search local documents or knowledge."""
        return bool(self.document_ids or self.knowledge_base_ids or self.collection_ids)


class DocumentStatus(str, Enum):
    """Durable ingestion state shown to users."""

    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"


class DocumentSummary(BaseModel):
    """Owner-scoped document metadata returned by the API."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    status: DocumentStatus
    failure_code: str | None = None
    page_count: int | None = None
    chunk_count: int = 0
    ocr_pages: int = 0
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    current_generation_id: str | None = None


class DocumentChunkView(BaseModel):
    """One source-located extracted chunk safe to display to its owner."""

    id: str
    document_id: str
    ordinal: int
    locator: str
    heading: str | None = None
    text: str


def selection_from_config(config: dict | None) -> SourceSelection:
    """Load the frozen selection from RunnableConfig metadata."""
    payload = dict((config or {}).get("metadata") or {}).get("source_selection")
    return SourceSelection.model_validate(payload or {"mode": "web", "sources": []})
