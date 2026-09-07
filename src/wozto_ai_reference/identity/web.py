"""HTTP surface for the login flow and two demo resource routes.

Everything here is a thin shell. The router decides nothing: `oidc.py` validates the
token, `session.py` owns rotation and CSRF, `authz.py` resolves the mailbox and writes
the ledger row. A route that made its own authorization decision would be a second place
to keep the rules right.

## Fail-closed shape of every handler

1. no session, or a session with no principal -> **401**, before anything else;
2. state-changing routes (`/auth/logout`, `POST .../drafts`) require the session's CSRF
   token -> **403** without it, *before* the authorization check runs;
3. authorization deny -> **403** carrying the machine reason, never a free-text message;
4. any identity error surfaces as its `reason` code and nothing else — an exception
   message could carry a code, a token or a claim value into the response body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Body, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from ._support import WallClock, system_wall_clock
from .authz import (
    DecisionLedger,
    GrantTable,
    MailboxAction,
    MailboxResource,
    authorize,
)
from .errors import IdentityError
from .oidc import OidcIdentityProvider
from .session import PendingLogin, SessionManager, SessionRecord

CSRF_HEADER = "X-CSRF-Token"
REQUEST_ID_HEADER = "X-Request-ID"


class DraftPayload(BaseModel):
    """A draft body. `mailbox_id` is deliberately absent — it is never taken from input."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)


@dataclass(frozen=True)
class IdentityWeb:
    """Everything the router needs, injected. No module-level state, no globals."""

    provider: OidcIdentityProvider
    sessions: SessionManager
    grants: GrantTable
    ledger: DecisionLedger
    clock: WallClock = system_wall_clock
    post_login_redirect: str = "/me"


def _request_id(header_value: str | None) -> str:
    candidate = (header_value or "").strip()
    # Serbest metin bir istek kimliği deftere gider; uzunluğu ve içeriği sınırlanır.
    if candidate and len(candidate) <= 128 and candidate.isprintable():
        return candidate
    return uuid4().hex


def build_identity_router(web: IdentityWeb) -> APIRouter:
    """Wire the routes.

    🔴 Çerez `Request`ten ELLE okunuyor, `Cookie(alias=...)` ile DEĞİL — ve bu bir üslup
    tercihi değil, ölçülmüş bir arızanın düzeltmesidir. Bu modülde
    `from __future__ import annotations` var, yani her annotation bir DİZGİdir ve FastAPI
    onu modül global'lerinde `eval` eder. `Annotated[str | None, Cookie(alias=web.sessions.
    cookie_name)]` içindeki `web` bir KAPANIŞ (closure) değişkenidir, modül global'i
    değil — çözülemez. FastAPI o parametreyi sessizce sıradan bir parametreye düşürdü ve
    çerezi **query parametresi** olarak sınıflandırdı: oturum çerezi handler'a HİÇ
    ulaşmadı, `/me` sürekli 401 döndü ve hiçbir kapı ötmedi. Çerez adı zaten
    yapılandırılabilir olduğu için statik bir annotation onu ifade edemez de.
    (`X-CSRF-Token` / `X-Request-ID` başlıkları modül düzeyi SABİTLER kullanır, bu yüzden
    `Header(alias=...)` orada güvenle çözülür.)
    """

    router = APIRouter()

    def _cookie(request: Request) -> str | None:
        return request.cookies.get(web.sessions.cookie_name)

    async def _require_session(cookie_value: str | None) -> SessionRecord:
        record = await web.sessions.load(cookie_value=cookie_value)
        if record is None or record.principal is None:
            raise HTTPException(status_code=401, detail="no_authenticated_session")
        return record

    def _authorize_or_403(record: SessionRecord, mailbox_id: str, action: MailboxAction, request_id: str) -> Any:
        assert record.principal is not None  # guaranteed by _require_session
        decision = authorize(
            record.principal,
            MailboxResource(requested_mailbox_id=mailbox_id),
            action,
            grants=web.grants,
            ledger=web.ledger,
            request_id=request_id,
            clock=web.clock,
        )
        if not decision.allowed:
            raise HTTPException(status_code=403, detail=decision.reason)
        return decision

    # ------------------------------------------------------------------ login flow

    @router.get("/auth/login")
    async def login() -> Response:
        try:
            authorization = await web.provider.begin_authorization()
        except IdentityError as exc:
            raise HTTPException(status_code=503, detail=exc.reason) from exc
        record = await web.sessions.begin_login(
            pending=PendingLogin(
                state=authorization.state,
                nonce=authorization.nonce,
                code_verifier=authorization.code_verifier,
            )
        )
        response = RedirectResponse(authorization.authorization_url, status_code=303)
        web.sessions.attach_cookie(response, record)
        return response

    @router.get("/auth/callback")
    async def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        record = await web.sessions.load(cookie_value=_cookie(request))
        if record is None or record.pending_login is None:
            raise HTTPException(status_code=400, detail="no_login_in_progress")

        # `state`, `nonce` ve doğrulayıcı TEK KULLANIMLIKTIR. Sonuç ne olursa olsun
        # bekleyen oturum burada yakılır; başarısız bir denemeden sonra aynı `state`i
        # yeniden oynatmak mümkün olmamalı.
        pending = record.pending_login
        await web.sessions.logout(session_id=record.session_id)

        if error:
            raise HTTPException(status_code=400, detail="authorization_request_refused")
        if not code or not state:
            raise HTTPException(status_code=400, detail="incomplete_authorization_response")

        try:
            principal = await web.provider.complete_authorization(
                code=code,
                state=state,
                expected_state=pending.state,
                expected_nonce=pending.nonce,
                code_verifier=pending.code_verifier,
            )
        except IdentityError as exc:
            raise HTTPException(status_code=400, detail=exc.reason) from exc

        # Rotation: eski kayıt yukarıda silindi, burada YENİ kimlik basılıyor.
        new_record = await web.sessions.complete_login(previous_session_id=None, principal=principal)
        response = RedirectResponse(web.post_login_redirect, status_code=303)
        web.sessions.attach_cookie(response, new_record)
        return response

    @router.post("/auth/logout", status_code=204)
    async def logout(
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
    ) -> Response:
        record = await _require_session(_cookie(request))
        try:
            web.sessions.verify_csrf(record, csrf_token)
        except IdentityError as exc:
            raise HTTPException(status_code=403, detail=exc.reason) from exc
        await web.sessions.logout(session_id=record.session_id)
        response = Response(status_code=204)
        web.sessions.clear_cookie(response)
        return response

    @router.get("/me")
    async def me(
        request: Request,
    ) -> JSONResponse:
        record = await _require_session(_cookie(request))
        principal = record.principal
        assert principal is not None
        return JSONResponse(
            {
                "tenant_id": principal.tenant_id,
                "user_id": principal.user_id,
                "roles": sorted(principal.roles),
                "csrf_token": record.csrf_token,
                "grants": [
                    {"mailbox_id": grant.mailbox_id, "role": grant.role}
                    for grant in web.grants.grants_of(principal.user_id)
                ],
            }
        )

    # ------------------------------------------------------------------ demo resources

    @router.get("/mailboxes/{mailbox_id}/summary")
    async def mailbox_summary(
        mailbox_id: str,
        request: Request,
        request_id_header: Annotated[str | None, Header(alias=REQUEST_ID_HEADER)] = None,
    ) -> JSONResponse:
        record = await _require_session(_cookie(request))
        request_id = _request_id(request_id_header)
        decision = _authorize_or_403(record, mailbox_id, "view_summary", request_id)
        return JSONResponse(
            {
                # Karardan gelen kimlik döner, istekten gelen DEĞİL.
                "mailbox_id": decision.mailbox_id,
                "role": decision.role,
                "request_id": request_id,
                # ⛔ Uydurma sayı yok. Bu spike'ta bağlı bir mesaj deposu YOKTUR ve
                # olmayan bir sayıyı "3 okunmamış" diye yazmak, tam da bu deponun
                # kaçındığı şeydir.
                "summary": "no message store is wired in this spike; this route proves the authorization path only",
            }
        )

    @router.post("/mailboxes/{mailbox_id}/drafts", status_code=201)
    async def create_draft(
        mailbox_id: str,
        payload: Annotated[DraftPayload, Body()],
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
        request_id_header: Annotated[str | None, Header(alias=REQUEST_ID_HEADER)] = None,
    ) -> JSONResponse:
        record = await _require_session(_cookie(request))
        # CSRF, yetkiden ÖNCE. Sıra önemli: geçersiz bir CSRF ile gelen istek deftere
        # bir yetki kararı yazmamalı, çünkü o istek hiç değerlendirilmedi.
        try:
            web.sessions.verify_csrf(record, csrf_token)
        except IdentityError as exc:
            raise HTTPException(status_code=403, detail=exc.reason) from exc
        request_id = _request_id(request_id_header)
        decision = _authorize_or_403(record, mailbox_id, "draft", request_id)
        return JSONResponse(
            status_code=201,
            content={
                "draft_id": uuid4().hex,
                "mailbox_id": decision.mailbox_id,
                "subject": payload.subject,
                "request_id": request_id,
                "persisted": False,
            },
        )

    return router
