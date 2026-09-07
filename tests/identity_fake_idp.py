"""An in-process OpenID Provider for hermetic tests. No network, no container, no clock drift.

Served through `httpx.ASGITransport`, so the relying party under test makes *real* HTTP
requests (real headers, real form encoding, real status codes) that never leave the
process. Paths mirror Keycloak's real shape (`/realms/<realm>/protocol/openid-connect/...`)
so the double does not quietly teach the code a URL layout no IdP uses.

Every deviation a test needs is an explicit switch on the instance, and each one exists
because a *specific* rejection has to be provable:

| switch | what it forges |
|---|---|
| `nonce_override` | an ID token whose `nonce` is not the one this login started |
| `issuer_override` | `iss` from a different issuer |
| `audience_override` | `aud` for a different client |
| `lifetime_seconds` | a negative value produces an already-expired token |
| `sign_with_unpublished_key` | a `kid` the JWKS does not contain (rotation, or forgery) |
| `publish_rotated_key()` | makes that `kid` appear — the **positive control** for the refetch |

`request_counts` is read at the wire. "Exactly one JWKS refetch" is measured from the
provider's own HTTP traffic, not from a counter the provider reports about itself.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from joserfc import jwk, jwt

REALM_PATH = "/realms/drill"


class FrozenClock:
    """A wall clock the test moves by hand. `advance()` is the only way time passes."""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("frozen clock needs a timezone-aware datetime")
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


@dataclass
class FakeUser:
    subject: str
    username: str
    roles: tuple[str, ...] = ()
    email: str | None = None


@dataclass
class _PendingCode:
    subject: str
    nonce: str
    code_challenge: str
    redirect_uri: str
    # Token damgası AUTHORIZE anında sabitlenir, exchange anında değil. Böylece test,
    # `_authorize()` ile `complete_authorization()` arasında saati oynatarak yalnız
    # DOĞRULAYAN tarafın saatini kaydırabilir — gerçek hayatta olan da budur (token
    # bir kez damgalanır, sonra doğrulanır).
    issued_at: int


@dataclass
class FakeIdp:
    issuer_base: str = "https://idp.test"
    realm: str = "drill"
    client_id: str = "drill-app"
    redirect_uri: str = "https://rp.test/auth/callback"
    users: dict[str, FakeUser] = field(default_factory=dict)
    clock: FrozenClock = field(default_factory=lambda: FrozenClock(datetime(2026, 9, 6, 12, 0, tzinfo=UTC)))

    # forgery switches
    nonce_override: str | None = None
    issuer_override: str | None = None
    # Keşif BELGESİNİN ilan ettiği issuer — token'daki `iss`ten AYRI bir düğme, ve ayrı
    # olmak ZORUNDA: belge doğru yolda servis edilmeye devam eder (yani 404 değil,
    # gerçekten bulunur) ama içinde başka bir issuer taşır. Tek düğme olsaydı test
    # "eşleşme kontrolü" yerine "sayfa yok"u ölçerdi — nitekim ilk hâli tam olarak
    # bunu yapıyordu.
    advertised_issuer_override: str | None = None
    audience_override: str | None = None
    lifetime_seconds: float = 300.0
    sign_with_unpublished_key: bool = False

    def __post_init__(self) -> None:
        self.signing_key = jwk.RSAKey.generate_key(2048, parameters={"kid": "primary", "use": "sig", "alg": "RS256"})
        self.rotated_key = jwk.RSAKey.generate_key(2048, parameters={"kid": "rotated", "use": "sig", "alg": "RS256"})
        self._published: list[Any] = [self.signing_key]
        self._codes: dict[str, _PendingCode] = {}
        self._access_tokens: dict[str, str] = {}
        self.request_counts: dict[str, int] = {}
        self.app = self._build_app()

    # ------------------------------------------------------------------ helpers

    @property
    def issuer(self) -> str:
        return f"{self.issuer_base}{REALM_PATH}"

    @property
    def jwks_uri(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/certs"

    def publish_rotated_key(self) -> None:
        """Make the rotated `kid` resolvable. The positive control for a JWKS refetch."""

        if self.rotated_key not in self._published:
            self._published.append(self.rotated_key)

    def _count(self, name: str) -> None:
        self.request_counts[name] = self.request_counts.get(name, 0) + 1

    def _id_token(self, *, user: FakeUser, nonce: str, issued_at: int) -> str:
        key = self.rotated_key if self.sign_with_unpublished_key else self.signing_key
        claims: dict[str, Any] = {
            "iss": self.issuer_override or self.issuer,
            "sub": user.subject,
            "aud": self.audience_override or self.client_id,
            "exp": issued_at + int(self.lifetime_seconds),
            "iat": issued_at,
            "nbf": issued_at,
            "nonce": self.nonce_override if self.nonce_override is not None else nonce,
            "preferred_username": user.username,
            "realm_access": {"roles": list(user.roles)},
        }
        if user.email:
            claims["email"] = user.email
        return jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)

    # ------------------------------------------------------------------ routes

    def _build_app(self) -> FastAPI:
        app = FastAPI()
        prefix = f"{REALM_PATH}/protocol/openid-connect"

        @app.get(f"{REALM_PATH}/.well-known/openid-configuration")
        async def discovery() -> JSONResponse:
            self._count("discovery")
            return JSONResponse(
                {
                    "issuer": self.advertised_issuer_override or self.issuer,
                    "authorization_endpoint": f"{self.issuer}/protocol/openid-connect/auth",
                    "token_endpoint": f"{self.issuer}/protocol/openid-connect/token",
                    "jwks_uri": self.jwks_uri,
                    "userinfo_endpoint": f"{self.issuer}/protocol/openid-connect/userinfo",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                }
            )

        @app.get(f"{prefix}/certs")
        async def certs() -> JSONResponse:
            self._count("jwks")
            return JSONResponse(jwk.KeySet(self._published).as_dict(private=False))

        @app.get(f"{prefix}/auth")
        async def authorize(request: Request) -> Any:
            """Auto-consent. A login form would test the form, not the protocol."""

            self._count("authorize")
            params = request.query_params
            if params.get("client_id") != self.client_id:
                return JSONResponse({"error": "unauthorized_client"}, status_code=400)
            if params.get("redirect_uri") != self.redirect_uri:
                # Gerçek bir IdP burada YÖNLENDİRMEZ; kaydedilmemiş bir redirect_uri'ye
                # hata göndermek açık yönlendirme (open redirect) olurdu.
                return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
            if params.get("code_challenge_method") != "S256":
                return JSONResponse({"error": "invalid_request", "detail": "S256 required"}, status_code=400)
            username = params.get("login_as") or next(iter(self.users))
            user = self.users[username]
            code = uuid4().hex
            self._codes[code] = _PendingCode(
                subject=user.subject,
                nonce=params.get("nonce", ""),
                code_challenge=params.get("code_challenge", ""),
                redirect_uri=params["redirect_uri"],
                issued_at=int(self.clock().timestamp()),
            )
            location = f"{self.redirect_uri}?{urlencode({'code': code, 'state': params.get('state', '')})}"
            return RedirectResponse(location, status_code=303)

        @app.post(f"{prefix}/token")
        async def token(request: Request) -> JSONResponse:
            self._count("token")
            # Gövde ELLE ayrıştırılıyor: FastAPI'nin `Form(...)`u `python-multipart`
            # ister ve o paket bu deponun ne çekirdek ne de `auth` bağımlılığıdır.
            # Test ikizi için üretim bağımlılık listesi genişletilmez.
            form = {
                key: values[0]
                for key, values in parse_qs((await request.body()).decode("utf-8")).items()
                if values
            }
            grant_type = form.get("grant_type", "")
            code = form.get("code", "")
            redirect_uri = form.get("redirect_uri", "")
            client_id = form.get("client_id", "")
            code_verifier = form.get("code_verifier", "")
            if grant_type != "authorization_code" or client_id != self.client_id:
                return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
            pending = self._codes.pop(code, None)  # kodlar TEK KULLANIMLIK
            if pending is None or pending.redirect_uri != redirect_uri:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            expected = (
                base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
                .decode("ascii")
                .rstrip("=")
            )
            if expected != pending.code_challenge:
                return JSONResponse({"error": "invalid_grant", "detail": "pkce"}, status_code=400)
            user = next(u for u in self.users.values() if u.subject == pending.subject)
            access_token = uuid4().hex
            self._access_tokens[access_token] = user.subject
            return JSONResponse(
                {
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "expires_in": int(self.lifetime_seconds),
                    "id_token": self._id_token(
                        user=user, nonce=pending.nonce, issued_at=pending.issued_at
                    ),
                }
            )

        @app.get(f"{prefix}/userinfo")
        async def userinfo(request: Request) -> JSONResponse:
            self._count("userinfo")
            header = request.headers.get("authorization", "")
            token_value = header.removeprefix("Bearer ").strip()
            subject = self._access_tokens.get(token_value)
            if subject is None:
                return JSONResponse({"error": "invalid_token"}, status_code=401)
            user = next(u for u in self.users.values() if u.subject == subject)
            return JSONResponse({"sub": user.subject, "preferred_username": user.username})

        return app


def drill_users() -> dict[str, FakeUser]:
    """The three principals the authorization tests and the live drill both use."""

    return {
        "alice": FakeUser(subject="alice-sub", username="alice", roles=("mailbox-user",)),
        "bob": FakeUser(subject="bob-sub", username="bob", roles=("mailbox-user",)),
        "mia": FakeUser(subject="mia-sub", username="mia", roles=("mailbox-manager",)),
    }
