"""Provider-independent domain contracts for the portable AI reference."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

NonEmptyText = Annotated[str, Field(min_length=1)]


class Principal(BaseModel):
    """Authenticated caller identity after an identity adapter has resolved it."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    tenant_id: NonEmptyText
    user_id: NonEmptyText
    roles: frozenset[str] = Field(default_factory=frozenset)


class Document(BaseModel):
    """Versioned source document available to a retrieval adapter."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    tenant_id: NonEmptyText
    document_id: NonEmptyText
    version: NonEmptyText
    title: NonEmptyText
    section: NonEmptyText
    source_uri: NonEmptyText
    content: NonEmptyText
    content_hash: NonEmptyText
    acl_roles: frozenset[str] = Field(default_factory=frozenset)


class RetrievalHit(BaseModel):
    """A scored source returned by a search provider."""

    model_config = ConfigDict(frozen=True)

    document: Document
    score: float = Field(ge=0.0)


class Citation(BaseModel):
    """Provenance emitted to the caller for an authorized retrieval hit."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    version: str
    title: str
    section: str
    source_uri: str
    content_hash: str
    score: float


class QueryPayload(BaseModel):
    """HTTP request body; identity is resolved outside this payload."""

    model_config = ConfigDict(str_strip_whitespace=True)

    query: NonEmptyText
    top_k: int = Field(default=5, ge=1, le=20)


class QueryResult(BaseModel):
    """Grounded answer or a fail-closed abstention."""

    answer: str
    citations: list[Citation]
    abstained: bool
    trace_id: str


class TelemetryEvent(BaseModel):
    """Vendor-neutral structured event emitted by the orchestration layer."""

    name: str
    trace_id: str
    tenant_id: str
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)
