"""Ports implemented by local, cloud, or on-prem provider adapters."""

from collections.abc import Sequence
from typing import Literal, Protocol

from .domain import (
    Document,
    EvidenceSupportDecision,
    ModelOutput,
    Principal,
    QueryPolicyDecision,
    QueryScopeDecision,
    RetrievalHit,
    TelemetryEvent,
)

# Sorgu ve belge AYNI MODELE farkli girdi olarak verilir. Simetrik saglayicilar
# (ornegin token-hash) bu ayrimi yok sayar; asimetrik modeller (e5, BGE, GTE...)
# yok saymaz ve prefix yanlis olursa geri getirme kalitesi OLCULEBILIR bicimde duser.
# Varsayilan "passage": mevcut tum cagri yerleri belge gomuyordu, davranis degismez.
EmbeddingKind = Literal["query", "passage"]


class IdentityUnavailable(RuntimeError):
    """Raised when no trusted identity adapter is available."""


class ModelProvider(Protocol):
    async def generate(
        self,
        *,
        query: str,
        hits: Sequence[RetrievalHit],
        trace_id: str,
    ) -> ModelOutput: ...


class EmbeddingProvider(Protocol):
    @property
    def dimensions(self) -> int: ...

    @property
    def model_id(self) -> str:
        """Gomme UZAYININ kimligi (model + revizyon). Kalicilastirilan her vektorun
        yanina yazilir. Saglayici degisirse eski satirlar eski uzayda kalir ve BOYUT
        AYNIYSA sessizce anlamsiz benzerlik doner — kimlik bunu gorunur kilar."""
        ...

    async def embed(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind = "passage",
    ) -> Sequence[Sequence[float]]: ...


class SearchProvider(Protocol):
    async def search(
        self,
        *,
        principal: Principal,
        query: str,
        limit: int,
    ) -> Sequence[RetrievalHit]: ...


class QueryPolicy(Protocol):
    def evaluate(self, *, principal: Principal, query: str) -> QueryPolicyDecision: ...


class QueryScopeResolver(Protocol):
    async def resolve(self, *, principal: Principal, query: str) -> QueryScopeDecision: ...


class EvidenceSupportCritic(Protocol):
    async def evaluate(
        self,
        *,
        principal: Principal,
        query: str,
        answer: ModelOutput,
        hits: Sequence[RetrievalHit],
    ) -> EvidenceSupportDecision: ...


class DocumentStore(Protocol):
    async def upsert(self, document: Document) -> None: ...

    async def delete(self, *, tenant_id: str, document_id: str, version: str) -> None: ...

    async def replace_source(
        self,
        *,
        tenant_id: str,
        source_document_id: str,
        documents: Sequence[Document],
    ) -> None: ...


class IdentityProvider(Protocol):
    @property
    def ready(self) -> bool: ...

    async def resolve(
        self,
        *,
        tenant_id: str | None,
        user_id: str | None,
        roles: str | None,
    ) -> Principal: ...


class TelemetryProvider(Protocol):
    def record(self, event: TelemetryEvent) -> None: ...
