"""Deterministic local adapters used before any live cloud resource exists."""

import re
from collections.abc import Iterable, Sequence

from .domain import Document, Principal, RetrievalHit, TelemetryEvent
from .ports import IdentityUnavailable

_TERM_PATTERN = re.compile(r"[\wçğıöşü]+", re.IGNORECASE)


def _terms(value: str) -> set[str]:
    return {term.casefold() for term in _TERM_PATTERN.findall(value)}


def is_authorized(principal: Principal, document: Document) -> bool:
    """Return whether the principal may retrieve the document."""

    if document.tenant_id != principal.tenant_id:
        return False
    return not document.acl_roles or bool(document.acl_roles.intersection(principal.roles))


class InMemorySearchProvider:
    """Small deterministic lexical adapter for tests and local architecture work."""

    def __init__(self, documents: Iterable[Document]) -> None:
        self._documents = tuple(documents)

    async def search(
        self,
        *,
        principal: Principal,
        query: str,
        limit: int,
    ) -> Sequence[RetrievalHit]:
        query_terms = _terms(query)
        if not query_terms:
            return []

        hits: list[RetrievalHit] = []
        for document in self._documents:
            if not is_authorized(principal, document):
                continue
            document_terms = _terms(f"{document.title} {document.section} {document.content}")
            overlap = len(query_terms.intersection(document_terms))
            if overlap == 0:
                continue
            score = overlap / len(query_terms)
            hits.append(RetrievalHit(document=document, score=score))

        hits.sort(key=lambda hit: (-hit.score, hit.document.document_id, hit.document.version))
        return hits[:limit]


class DeterministicGroundedModel:
    """Non-LLM adapter that makes authorization and citation behavior testable."""

    async def generate(
        self,
        *,
        query: str,
        hits: Sequence[RetrievalHit],
        trace_id: str,
    ) -> str:
        del query, trace_id
        snippets = " ".join(hit.document.content for hit in hits)
        return f"Yetkili kaynaklara göre: {snippets}"


class LocalHeaderIdentityProvider:
    """Explicitly enabled local-only identity adapter; never a production trust boundary."""

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled

    @property
    def ready(self) -> bool:
        return self._enabled

    async def resolve(
        self,
        *,
        tenant_id: str | None,
        user_id: str | None,
        roles: str | None,
    ) -> Principal:
        if not self._enabled:
            raise IdentityUnavailable("Local header identity is disabled")
        if not tenant_id or not user_id:
            raise ValueError("X-Tenant-ID and X-User-ID are required")
        parsed_roles = frozenset(role.strip() for role in (roles or "").split(",") if role.strip())
        return Principal(tenant_id=tenant_id, user_id=user_id, roles=parsed_roles)


class MemoryTelemetry:
    """Collect structured telemetry locally without exporting customer data."""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []

    def record(self, event: TelemetryEvent) -> None:
        self.events.append(event)
