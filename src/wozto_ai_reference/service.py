"""Provider-neutral query orchestration with defense-in-depth authorization."""

from uuid import uuid4

from .adapters import is_authorized
from .domain import Citation, Principal, QueryResult, RetrievalHit, TelemetryEvent
from .ports import ModelProvider, SearchProvider, TelemetryProvider

_ABSTAIN_MESSAGE = "Yeterli ve yetkili kaynak bulunamadı."


class QueryService:
    def __init__(
        self,
        *,
        search: SearchProvider,
        model: ModelProvider,
        telemetry: TelemetryProvider,
        minimum_score: float = 0.01,
    ) -> None:
        self._search = search
        self._model = model
        self._telemetry = telemetry
        self._minimum_score = minimum_score

    async def query(self, *, principal: Principal, query: str, limit: int = 5) -> QueryResult:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("query must not be empty")
        if limit < 1 or limit > 20:
            raise ValueError("limit must be between 1 and 20")

        trace_id = uuid4().hex
        self._record("query.started", trace_id, principal, {"limit": limit})

        provider_hits = await self._search.search(
            principal=principal,
            query=clean_query,
            limit=limit,
        )
        authorized_hits = [
            hit
            for hit in provider_hits
            if hit.score >= self._minimum_score and is_authorized(principal, hit.document)
        ][:limit]

        if not authorized_hits:
            self._record("query.abstained", trace_id, principal, {"authorized_hits": 0})
            return QueryResult(
                answer=_ABSTAIN_MESSAGE,
                citations=[],
                abstained=True,
                trace_id=trace_id,
            )

        answer = await self._model.generate(query=clean_query, hits=authorized_hits, trace_id=trace_id)
        citations = [self._citation(hit) for hit in authorized_hits]
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
        )
