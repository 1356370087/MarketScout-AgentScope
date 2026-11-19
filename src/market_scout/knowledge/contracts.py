"""Knowledge-base contracts for KB-01 libraries, collections and links."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeBaseCreate(BaseModel):
    """One knowledge base; defaults to the caller's personal workspace."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    workspace_id: str | None = Field(default=None, max_length=64)


class KnowledgeBaseUpdate(BaseModel):
    """Rename or re-describe a knowledge base; at least one field required."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_field(self) -> KnowledgeBaseUpdate:
        """Reject empty patch bodies instead of touching updated_at blindly."""
        if self.name is None and self.description is None:
            raise ValueError("knowledge_base_update_empty")
        return self


class KnowledgeBaseView(BaseModel):
    """Owner-visible knowledge-base summary with bounded aggregate counts."""

    id: str
    name: str
    description: str = ""
    archived: bool = False
    collection_count: int = 0
    document_count: int = 0
    created_at: str
    updated_at: str
    archived_at: str | None = None


class KnowledgeCollectionCreate(BaseModel):
    """One single-level collection inside a knowledge base."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


class KnowledgeCollectionUpdate(BaseModel):
    """Rename or re-describe a collection; at least one field required."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_field(self) -> KnowledgeCollectionUpdate:
        """Reject empty patch bodies."""
        if self.name is None and self.description is None:
            raise ValueError("knowledge_collection_update_empty")
        return self


class KnowledgeCollectionView(BaseModel):
    """Owner-visible collection summary."""

    id: str
    knowledge_base_id: str
    name: str
    description: str = ""
    document_count: int = 0
    created_at: str
    updated_at: str


class KnowledgeDocumentLinkRequest(BaseModel):
    """Associate existing owned documents with a knowledge base or collection."""

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(min_length=1, max_length=100)
    collection_id: str | None = Field(default=None, min_length=1, max_length=64)


class KnowledgeDocumentView(BaseModel):
    """One document placed inside a knowledge base scope."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    status: str
    chunk_count: int = 0
    added_at: str
