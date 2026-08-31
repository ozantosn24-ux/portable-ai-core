"""Provider-neutral query orchestration with defense-in-depth authorization."""

import math
from collections.abc import Sequence
from datetime import date
from uuid import uuid4

from .adapters import is_authorized
from .domain import (
    Citation,
    Document,
    Principal,
    QueryConstraints,
    QueryResult,
    RetrievalHit,
    SourceAuthority,
    SourceStatus,
    StructuredAnswer,
    TelemetryEvent,
    evidence_reference,
)
from .ports import (
    EvidenceSupportCritic,
    ModelProvider,
    QueryPolicy,
    QueryScopeResolver,
    SearchProvider,
    TelemetryProvider,
)

_ABSTAIN_MESSAGE = "Yeterli ve yetkili kaynak bulunamadı."


def eligible_hits(
    *,
    principal: Principal,
    hits: Sequence[RetrievalHit],
    minimum_score: float,
    limit: int,
    as_of: date | None = None,
    source_status: SourceStatus | None = None,
    source_authority: SourceAuthority | None = None,
) -> list[RetrievalHit]:
    """Apply the same score and authorization gate in serving and evaluation.

    `score` belongs to the search provider. A threshold is only meaningful after it
    has been calibrated for that provider/model/corpus; this helper deliberately does
    not invent a universal default.
    """

    if not math.isfinite(minimum_score) or minimum_score < 0.0:
        raise ValueError("minimum_score must be a finite non-negative number")
    if limit < 1:
        raise ValueError("limit must be positive")
    return [
        hit
        for hit in hits
        if hit.score >= minimum_score
        and is_authorized(principal, hit.document)
        and source_is_eligible(
            hit.document,
            as_of=as_of,
            source_status=source_status,
            source_authority=source_authority,
        )
    ][:limit]


def source_is_eligible(
    document: Document,
    *,
    as_of: date | None = None,
    source_status: SourceStatus | None = None,
    source_authority: SourceAuthority | None = None,
) -> bool:
    """Apply explicit caller-resolved source constraints; do not parse natural language."""

    if source_status is not None and document.source_status != source_status:
        return False
    if source_authority is not None and document.source_authority != source_authority:
        return False
    if as_of is None:
        return True
    if document.valid_from is None and document.valid_through is None:
        return False
    if document.valid_from is not None and as_of < document.valid_from:
        return False
    return document.valid_through is None or as_of <= document.valid_through


def merge_query_constraints(
    explicit: QueryConstraints,
    resolved: QueryConstraints,
) -> QueryConstraints | None:
    """Merge narrowing constraints; disagreement is an unsafe ambiguity."""

    merged: dict[str, object] = {}
    for field_name in ("as_of", "source_status", "source_authority"):
        explicit_value = getattr(explicit, field_name)
        resolved_value = getattr(resolved, field_name)
        if explicit_value is not None and resolved_value is not None and explicit_value != resolved_value:
            return None
        value = resolved_value if resolved_value is not None else explicit_value
        if value is not None:
            merged[field_name] = value
    return QueryConstraints(**merged)


class QueryService:
    def __init__(
        self,
        *,
        search: SearchProvider,
        model: ModelProvider,
        telemetry: TelemetryProvider,
        minimum_score: float = 0.01,
        query_policy: QueryPolicy | None = None,
        scope_resolver: QueryScopeResolver | None = None,
        evidence_critic: EvidenceSupportCritic | None = None,
    ) -> None:
        if not math.isfinite(minimum_score) or minimum_score < 0.0:
            raise ValueError("minimum_score must be a finite non-negative number")
        self._search = search
        self._model = model
        self._telemetry = telemetry
        self._minimum_score = minimum_score
        self._query_policy = query_policy
        self._scope_resolver = scope_resolver
        self._evidence_critic = evidence_critic

    async def query(
        self,
        *,
        principal: Principal,
        query: str,
        limit: int = 5,
        as_of: date | None = None,
        source_status: SourceStatus | None = None,
        source_authority: SourceAuthority | None = None,
    ) -> QueryResult:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("query must not be empty")
        if limit < 1 or limit > 20:
            raise ValueError("limit must be between 1 and 20")

        trace_id = uuid4().hex
        self._record("query.started", trace_id, principal, {"limit": limit})

        if self._query_policy is not None:
            policy = self._query_policy.evaluate(principal=principal, query=clean_query)
            if not policy.allowed:
                self._record(
                    "query.abstained",
                    trace_id,
                    principal,
                    {"reason": "request_policy", "policy_reason": policy.reason},
                )
                return QueryResult(
                    answer=_ABSTAIN_MESSAGE,
                    citations=[],
                    abstained=True,
                    trace_id=trace_id,
                )

        constraints = QueryConstraints(
            as_of=as_of,
            source_status=source_status,
            source_authority=source_authority,
        )
        if self._scope_resolver is not None:
            scope = await self._scope_resolver.resolve(principal=principal, query=clean_query)
            if not scope.allowed:
                self._record(
                    "query.abstained",
                    trace_id,
                    principal,
                    {"reason": "scope_resolution", "scope_reason": scope.reason},
                )
                return QueryResult(
                    answer=_ABSTAIN_MESSAGE,
                    citations=[],
                    abstained=True,
                    trace_id=trace_id,
                )
            merged_constraints = merge_query_constraints(constraints, scope.constraints)
            if merged_constraints is None:
                self._record(
                    "query.abstained",
                    trace_id,
                    principal,
                    {"reason": "scope_constraint_conflict"},
                )
                return QueryResult(
                    answer=_ABSTAIN_MESSAGE,
                    citations=[],
                    abstained=True,
                    trace_id=trace_id,
                )
            constraints = merged_constraints

        provider_hits = await self._search.search(
            principal=principal,
            query=clean_query,
            limit=limit,
        )
        authorized_hits = eligible_hits(
            principal=principal,
            hits=provider_hits,
            minimum_score=self._minimum_score,
            limit=limit,
            as_of=constraints.as_of,
            source_status=constraints.source_status,
            source_authority=constraints.source_authority,
        )

        if not authorized_hits:
            self._record("query.abstained", trace_id, principal, {"authorized_hits": 0})
            return QueryResult(
                answer=_ABSTAIN_MESSAGE,
                citations=[],
                abstained=True,
                trace_id=trace_id,
            )

        generated = await self._model.generate(query=clean_query, hits=authorized_hits, trace_id=trace_id)
        answer = generated.answer if isinstance(generated, StructuredAnswer) else generated
        citation_hits = authorized_hits
        if self._evidence_critic is not None:
            support = await self._evidence_critic.evaluate(
                principal=principal,
                query=clean_query,
                answer=generated,
                hits=authorized_hits,
            )
            authorized_evidence = {evidence_reference(hit.document) for hit in authorized_hits}
            if not support.supported or not support.supporting_evidence.issubset(authorized_evidence):
                reason = support.reason if not support.supported else "critic_invalid_support"
                self._record(
                    "query.abstained",
                    trace_id,
                    principal,
                    {"reason": "evidence_support", "critic_reason": reason},
                )
                return QueryResult(
                    answer=_ABSTAIN_MESSAGE,
                    citations=[],
                    abstained=True,
                    trace_id=trace_id,
                )
            citation_hits = [
                hit for hit in authorized_hits if evidence_reference(hit.document) in support.supporting_evidence
            ]

        citations = [self._citation(hit) for hit in citation_hits]
        self._record(
            "query.completed",
            trace_id,
            principal,
            {"authorized_hits": len(authorized_hits), "citations": len(citations)},
        )
        return QueryResult(answer=answer, citations=citations, abstained=False, trace_id=trace_id)

    def _record(
        self,
        name: str,
        trace_id: str,
        principal: Principal,
        attributes: dict[str, str | int | float | bool],
    ) -> None:
        self._telemetry.record(
            TelemetryEvent(
                name=name,
                trace_id=trace_id,
                tenant_id=principal.tenant_id,
                attributes=attributes,
            )
        )

    @staticmethod
    def _citation(hit: RetrievalHit) -> Citation:
        document = hit.document
        return Citation(
            document_id=document.document_id,
            version=document.version,
            title=document.title,
            section=document.section,
            source_uri=document.source_uri,
            content_hash=document.content_hash,
            score=hit.score,
            source_status=document.source_status,
            source_authority=document.source_authority,
            valid_from=document.valid_from,
            valid_through=document.valid_through,
        )
