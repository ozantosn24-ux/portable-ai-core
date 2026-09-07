"""Server-side session store, signed opaque cookie, rotation on login, per-session CSRF.

## Neden sunucu tarafı?

Cookie'de taşınan tek şey **opak bir oturum kimliğidir**. Principal, roller, PKCE
doğrulayıcısı, `state` ve `nonce` sunucuda durur. Bunun sebebi gizlilik değil
**iptal edilebilirliktir**: kendi kendini taşıyan (self-contained) bir cookie, sunucu
"bu oturumu kapat" dediğinde hâlâ geçerlidir — çıkışın gerçekten çıkış olması için
otoritenin sunucuda olması gerekir. `logout` testi tam olarak bunu ölçer.

Cookie yine de İMZALANIR. İmza kimliği "doğru" yapmaz (doğruluk depodan gelir); rastgele
kimlik denemelerini imza doğrulamasında, depoya hiç bakmadan keser.

## Neden login'de kimlik DEĞİŞİR (rotation)?

Session fixation: saldırgan kurbanın tarayıcısına önceden bir oturum kimliği yerleştirir,
kurban o kimlikle giriş yapar ve saldırgan artık kimliği doğrulanmış bir oturuma sahiptir.
Tek yapısal savunma, kimlik doğrulandığı ANDA kimliği değiştirmek ve eskisini SUNUCUDAN
silmektir — ikisi birden. Yalnız yeni cookie yazmak, eski kaydı canlı bırakır.

## Neden `SessionStore` bir Protocol?

Varsayılan bellek-içi depo tek süreçte doğrudur ve yeniden başlatmada her oturumu düşürür.
Gerçek bir kurulum Redis/Postgres ister. Protocol **async**tır: senkron bir imza, ilk
gerçek depoda baştan yazılmak zorunda kalırdı.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from ..domain import Principal
from ._support import WallClock, constant_time_equals, system_wall_clock
from .errors import SessionError

DEFAULT_COOKIE_NAME = "wozto_session"
_SESSION_ID_BYTES = 32
_CSRF_BYTES = 32
_MIN_SECRET_BYTES = 32


@dataclass(frozen=True)
class PendingLogin:
    """The three single-use values a login attempt must survive the redirect with."""

    state: str
    nonce: str
    code_verifier: str


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime
    principal: Principal | None = None
    pending_login: PendingLogin | None = None

    @property
    def authenticated(self) -> bool:
        return self.principal is not None


class SessionStore(Protocol):
    async def save(self, record: SessionRecord) -> None: ...

    async def load(self, session_id: str) -> SessionRecord | None: ...

    async def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    """Single-process default. Every session is lost on restart — by design, not by accident."""

    def __init__(self) -> None:
        self._records: dict[str, SessionRecord] = {}

    async def save(self, record: SessionRecord) -> None:
        self._records[record.session_id] = record

    async def load(self, session_id: str) -> SessionRecord | None:
        return self._records.get(session_id)

    async def delete(self, session_id: str) -> None:
        self._records.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._records)


class SignedCookieCodec:
    """`<session_id>.<base64url(HMAC-SHA256(session_id))>`.

    Stdlib only — `itsdangerous` would be a dependency for thirty lines. Session ids are
    `secrets.token_urlsafe`, which never contains a `.`, so `rpartition` splits cleanly.
    """

    def __init__(self, secret: bytes) -> None:
        if len(secret) < _MIN_SECRET_BYTES:
            raise SessionError(
                "session_secret_too_short",
                detail=f"session signing secret must be at least {_MIN_SECRET_BYTES} bytes",
            )
        self._secret = secret

    def _mac(self, session_id: str) -> str:
        digest = hmac.new(self._secret, session_id.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def sign(self, session_id: str) -> str:
        return f"{session_id}.{self._mac(session_id)}"

    def unsign(self, value: str) -> str | None:
        """Return the session id, or `None` for anything that does not verify."""

        session_id, separator, signature = value.rpartition(".")
        if not separator or not session_id or not signature:
            return None
        # Sabit zamanlı karşılaştırma: yoksa MAC baytları zamanlama farkından tek tek
        # çıkarılabilir. ⚠️ Doğrudan `hmac.compare_digest` DEĞİL — çerez baytları
        # Starlette'te latin-1 çözülür ve tek bir `\xff` baytı onu TypeError'a düşürür;
        # o da 401 yerine **500** demektir (bkz. `_support.constant_time_equals`).
        if not constant_time_equals(signature, self._mac(session_id)):
            return None
        return session_id


class SessionManager:
    """Cookie <-> store plumbing plus the two rules that matter: rotation and CSRF."""

    def __init__(
        self,
        *,
        store: SessionStore,
        secret: bytes,
        clock: WallClock = system_wall_clock,
        ttl_seconds: float = 3600.0,
        cookie_name: str = DEFAULT_COOKIE_NAME,
        cookie_secure: bool = True,
        cookie_path: str = "/",
        token_urlsafe: Any = secrets.token_urlsafe,
    ) -> None:
        if ttl_seconds <= 0:
            raise SessionError("non_positive_session_ttl")
        self._store = store
        self._codec = SignedCookieCodec(secret)
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)
        self._cookie_name = cookie_name
        self._cookie_secure = cookie_secure
        self._cookie_path = cookie_path
        self._token_urlsafe = token_urlsafe

    @property
    def cookie_name(self) -> str:
        return self._cookie_name

    def cookie_value(self, record: SessionRecord) -> str:
        return self._codec.sign(record.session_id)

    def _new_record(
        self,
        *,
        principal: Principal | None,
        pending_login: PendingLogin | None,
    ) -> SessionRecord:
        now = self._clock()
        return SessionRecord(
            session_id=self._token_urlsafe(_SESSION_ID_BYTES),
            csrf_token=self._token_urlsafe(_CSRF_BYTES),
            created_at=now,
            expires_at=now + self._ttl,
            principal=principal,
            pending_login=pending_login,
        )

    async def begin_login(self, *, pending: PendingLogin) -> SessionRecord:
        record = self._new_record(principal=None, pending_login=pending)
        await self._store.save(record)
        return record

    async def complete_login(
        self,
        *,
        previous_session_id: str | None,
        principal: Principal,
    ) -> SessionRecord:
        """Mint a NEW id and delete the old record. Both halves, or it is not a defence."""

        if previous_session_id is not None:
            await self._store.delete(previous_session_id)
        record = self._new_record(principal=principal, pending_login=None)
        await self._store.save(record)
        return record

    async def load(self, *, cookie_value: str | None) -> SessionRecord | None:
        if not cookie_value:
            return None
        session_id = self._codec.unsign(cookie_value)
        if session_id is None:
            return None
        record = await self._store.load(session_id)
        if record is None:
            return None
        if self._clock() >= record.expires_at:
            # Süresi dolmuş kayıt yalnız reddedilmez, SİLİNİR: yoksa depo süresiz büyür
            # ve "yok" ile "süresi dolmuş" iki farklı gözlemlenebilir duruma ayrılır.
            await self._store.delete(session_id)
            return None
        return record

    async def logout(self, *, session_id: str) -> None:
        await self._store.delete(session_id)

    async def clear_pending_login(self, record: SessionRecord) -> SessionRecord:
        updated = replace(record, pending_login=None)
        await self._store.save(updated)
        return updated

    def verify_csrf(self, record: SessionRecord, presented: str | None) -> None:
        """Raise unless the presented token matches this session's token exactly."""

        # Aynı sebep: `X-CSRF-Token: \xe9\xe9\xe9` başlığı ham `hmac.compare_digest`i
        # TypeError'a düşürür ve 403 yerine 500 döndürürdü.
        if not presented or not constant_time_equals(presented, record.csrf_token):
            raise SessionError("csrf_token_invalid")

    # ------------------------------------------------------------------ cookie wiring

    def attach_cookie(self, response: Any, record: SessionRecord) -> None:
        response.set_cookie(
            key=self._cookie_name,
            value=self.cookie_value(record),
            max_age=int(self._ttl.total_seconds()),
            path=self._cookie_path,
            # HttpOnly: cookie'yi JavaScript'ten görünmez yapar; XSS oturumu doğrudan
            # çalamaz. SameSite=Lax: üçüncü taraf POST'unda cookie gönderilmez, ama
            # IdP'den GET ile dönen callback çalışmayı sürdürür (Strict olsaydı
            # callback'te cookie GELMEZDİ ve login hiç tamamlanamazdı).
            httponly=True,
            samesite="lax",
            secure=self._cookie_secure,
        )

    def clear_cookie(self, response: Any) -> None:
        response.delete_cookie(
            key=self._cookie_name,
            path=self._cookie_path,
            httponly=True,
            samesite="lax",
            secure=self._cookie_secure,
        )
