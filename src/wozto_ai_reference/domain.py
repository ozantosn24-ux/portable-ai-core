"""Provider-independent domain contracts for the portable AI reference."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonEmptyText = Annotated[str, Field(min_length=1)]
PolicyReasonCode = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$"),
]
SourceStatus = Literal["unspecified", "current", "historical", "reference"]
SourceAuthority = Literal["unspecified", "advisory", "authoritative"]


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
    source_status: SourceStatus = "unspecified"
    source_authority: SourceAuthority = "unspecified"
    valid_from: date | None = None
    valid_through: date | None = None

    @model_validator(mode="after")
    def validate_validity_window(self) -> Document:
        if self.valid_from is not None and self.valid_through is not None and self.valid_from > self.valid_through:
            raise ValueError("valid_from must not be after valid_through")
        return self


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
    source_status: SourceStatus
    source_authority: SourceAuthority
    valid_from: date | None
    valid_through: date | None


class QueryPayload(BaseModel):
    """HTTP request body; identity is resolved outside this payload."""

    model_config = ConfigDict(str_strip_whitespace=True)

    query: NonEmptyText
    top_k: int = Field(default=5, ge=1, le=20)
    as_of: date | None = None
    source_status: SourceStatus | None = None
    source_authority: SourceAuthority | None = None


class QueryPolicyDecision(BaseModel):
    """Fail-closed decision from an operator-configured request policy."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    allowed: bool
    # This value crosses into telemetry. Restrict it to a short machine code so a
    # policy adapter cannot accidentally echo request or credential text.
    reason: PolicyReasonCode


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
