"""FastAPI composition root for the local portable AI checkpoint."""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .adapters import (
    DeterministicGroundedModel,
    InMemorySearchProvider,
    LocalHeaderIdentityProvider,
    MemoryTelemetry,
)
from .domain import Document, QueryPayload, QueryResult
from .embedding import HashEmbeddingProvider
from .ports import IdentityProvider, IdentityUnavailable
from .service import QueryService

_LOCAL_IDENTITY_ENV = "WOZTO_REFERENCE_ALLOW_INSECURE_HEADERS"
_BACKEND_ENV = "WOZTO_REFERENCE_BACKEND"
_DATABASE_URL_ENV = "WOZTO_REFERENCE_DATABASE_URL"


def _demo_service() -> QueryService:
    documents = [
        Document(
            tenant_id="tenant-demo",
            document_id="refund-policy",
            version="v1",
            title="Refund Policy",
            section="Eligibility",
            source_uri="memory://tenant-demo/refund-policy#eligibility",
            content="Refund requests are reviewed by a human operator before any action.",
            content_hash="sha256:demo-refund-v1",
        ),
        Document(
            tenant_id="tenant-demo",
            document_id="finance-policy",
            version="v1",
            title="Finance Policy",
            section="Restricted",
            source_uri="memory://tenant-demo/finance-policy#restricted",
            content="Finance-only approval limits are restricted to the finance role.",
            content_hash="sha256:demo-finance-v1",
            acl_roles=frozenset({"finance"}),
        ),
    ]
    return QueryService(
        search=InMemorySearchProvider(documents),
        model=DeterministicGroundedModel(),
        telemetry=MemoryTelemetry(),
    )


def _configured_service() -> tuple[QueryService, Callable[[], Awaitable[None]] | None]:
    backend = os.getenv(_BACKEND_ENV, "memory").strip().casefold()
    if backend == "memory":
        return _demo_service(), None
    if backend != "pgvector":
        raise RuntimeError(f"Unsupported {_BACKEND_ENV}: {backend}")
    database_url = os.getenv(_DATABASE_URL_ENV, "").strip()
    if not database_url:
        raise RuntimeError(f"{_DATABASE_URL_ENV} is required when {_BACKEND_ENV}=pgvector")

    from .pgvector_store import PgVectorStore

    store = PgVectorStore(
        database_url=database_url,
        embeddings=HashEmbeddingProvider(),
    )
    return (
        QueryService(
            search=store,
            model=DeterministicGroundedModel(),
            telemetry=MemoryTelemetry(),
        ),
        store.initialize,
    )


def create_app(
    *,
    service: QueryService | None = None,
    identity: IdentityProvider | None = None,
    allow_insecure_identity: bool | None = None,
    initializer: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    if allow_insecure_identity is None:
        allow_insecure_identity = os.getenv(_LOCAL_IDENTITY_ENV) == "1"
    resolved_identity = identity or LocalHeaderIdentityProvider(enabled=allow_insecure_identity)
    if service is None:
        resolved_service, configured_initializer = _configured_service()
        initializer = initializer or configured_initializer
    else:
        resolved_service = service

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if initializer is not None:
            await initializer()
        yield

    app = FastAPI(title="Wozto Portable AI Core", version="0.2.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        if not resolved_identity.ready:
            return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "identity_disabled"})
        return JSONResponse(status_code=200, content={"status": "ready"})

    @app.post("/query", response_model=QueryResult)
    async def query(
        payload: QueryPayload,
        tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
        user_id: Annotated[str | None, Header(alias="X-User-ID")] = None,
        roles: Annotated[str | None, Header(alias="X-Roles")] = None,
    ) -> QueryResult:
        try:
            principal = await resolved_identity.resolve(tenant_id=tenant_id, user_id=user_id, roles=roles)
        except IdentityUnavailable as exc:
            raise HTTPException(status_code=503, detail="Trusted identity provider is unavailable") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await resolved_service.query(
            principal=principal,
            query=payload.query,
            limit=payload.top_k,
            as_of=payload.as_of,
            source_status=payload.source_status,
            source_authority=payload.source_authority,
        )

    return app


app = create_app()
