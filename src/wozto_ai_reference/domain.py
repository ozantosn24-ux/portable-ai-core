"""Provider-independent domain contracts for the portable AI reference."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonEmptyText = Annotated[str, Field(min_length=1)]
DecisionReasonCode = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$"),
]
# Backward-compatible public name from the first hard-policy checkpoint.
PolicyReasonCode = DecisionReasonCode
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


class EvidenceReference(BaseModel):
    """Version- and content-bound identity used by a post-generation critic."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    document_id: NonEmptyText
    version: NonEmptyText
    content_hash: NonEmptyText


def evidence_reference(document: Document) -> EvidenceReference:
    return EvidenceReference(
        document_id=document.document_id,
        version=document.version,
        content_hash=document.content_hash,
    )


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

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: NonEmptyText
    top_k: int = Field(default=5, ge=1, le=20)
    as_of: date | None = None
    source_status: SourceStatus | None = None
    source_authority: SourceAuthority | None = None


class QueryConstraints(BaseModel):
    """Caller- or resolver-supplied requirements that may only narrow evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: date | None = None
    source_status: SourceStatus | None = None
    source_authority: SourceAuthority | None = None

    @property
    def constrained(self) -> bool:
        return any(value is not None for value in (self.as_of, self.source_status, self.source_authority))


class QueryPolicyDecision(BaseModel):
    """Fail-closed decision from an operator-configured request policy."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    allowed: bool
    # This value crosses into telemetry. Restrict it to a short machine code so a
    # policy adapter cannot accidentally echo request or credential text.
    reason: DecisionReasonCode


class QueryScopeDecision(BaseModel):
    """Explainable scope resolution; a refusal carries no usable constraints."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    allowed: bool
    reason: DecisionReasonCode
    constraints: QueryConstraints = Field(default_factory=QueryConstraints)

    @model_validator(mode="after")
    def refused_scope_has_no_constraints(self) -> QueryScopeDecision:
        if not self.allowed and self.constraints.constrained:
            raise ValueError("a refused scope decision must not carry constraints")
        return self


class EvidenceSupportDecision(BaseModel):
    """Post-generation evidence verdict and the exact documents supporting it."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    supported: bool
    reason: DecisionReasonCode
    supporting_evidence: frozenset[EvidenceReference] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def validate_supporting_documents(self) -> EvidenceSupportDecision:
        if self.supported and not self.supporting_evidence:
            raise ValueError("a supported answer must identify supporting evidence")
        if not self.supported and self.supporting_evidence:
            raise ValueError("an unsupported answer must not identify supporting evidence")
        return self


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
