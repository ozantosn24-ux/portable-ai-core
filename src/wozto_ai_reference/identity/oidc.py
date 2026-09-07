"""OIDC authorization-code + PKCE relying party behind the `IdentityProvider` port.

## Kütüphane seçimi: `httpx` + `joserfc` (Authlib DEĞİL)

Authlib'in `AsyncOAuth2Client`'ı `httpx.AsyncClient` türevidir, yani testte
`ASGITransport` enjekte etmeye teknik olarak uygundur — seçim onun **çalışmamasından**
değil, bu modülün ihtiyaç yüzeyinden çıktı:

1. **Yüzey oranı.** Bu akışın ağ işi üç istektir: discovery GET, JWKS GET, token POST.
   Authlib bunun için bir OAuth *çerçevesi* getirir (çoklu grant tipleri, kendi oturum
   ve state saklaması, kayıt/keşif katmanı). Buradaki state/nonce/PKCE saklaması zaten
   `session.py`de sunucu tarafındadır; ikinci bir saklama yeri iki doğruluk kaynağı
   demektir.
2. **Kritik olan kısım el yazısı olmalı.** Bu modülün tek gerçek güvenlik işi ID token
   doğrulamasıdır ve orada üç şeyi açıkça istiyoruz: enjekte edilebilir saat, SINIRLI
   kayma (skew) ve her ret için ayrı bir makine kodu. Bunlar bir kütüphanenin
   varsayılanlarına bırakılırsa "neden reddedildi" sorusu tek bir genel istisnaya iner.
3. **Algoritma seçimi başlıktan okunmaz.** `joserfc.jwt.decode(..., algorithms=[...])`
   izin listesini ÇAĞRI YERİNDE ister; `alg` doğrulanmamış başlıktan gelmez. Bu modül
   ayrıca `alg`'i anahtarı aramadan ÖNCE listeye karşı sınar — `none` ve HS/RS karışıklığı
   sınıfı böylece imza kodu hiç çalışmadan kapanır.
4. **Tek HTTP yığını.** `httpx` zaten bu deponun test istemcisi; sahte IdP tek bir
   `ASGITransport` ile süreç içinde koşar, ikinci bir HTTP istemcisi eklenmez.

Bedeli dürüstçe: JWKS önbelleği, discovery önbelleği ve claim doğrulaması burada elle
yazılmıştır — yani bu kodun kendisi test edilmek zorundadır (bkz. `tests/test_identity_oidc.py`).

## `resolve()` neden fail-closed?

`IdentityProvider` portu `resolve(tenant_id=..., user_id=..., roles=...)` imzasını
taşır — bu **başlıktan** kimlik kuran bir imzadır. OIDC'de principal, imzalı bir ID
token'dan ve sunucu tarafındaki oturumdan doğar; istek başlıklarından DEĞİL. Bu yüzden
buradaki `resolve()` başarısız olur ve **hiçbir zaman** başlık değerine güvenmez. Portu
uygulamamak (ve `/ready` sözleşmesini kaybetmek) yerine, portu uygulayıp o yolu kapatmak
seçildi: `/ready` "güvenilir bir kimlik sağlayıcı bağlı mı" sorusunu sorar, "bu istekte
kim var" sorusunu değil.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

from ..domain import Principal
from ..ports import IdentityUnavailable
from ._support import (
    WallClock,
    constant_time_equals,
    epoch_seconds,
    import_optional,
    system_wall_clock,
)
from .errors import (
    AuthorizationFlowError,
    InvalidIdToken,
    OidcConfigurationError,
    OidcDiscoveryError,
    TokenEndpointError,
)

TokenEndpointAuthMethod = Literal["none", "client_secret_basic", "client_secret_post"]

# PKCE code verifier: RFC 7636 §4.1, 43-128 karakter unreserved. 32 bayt entropi
# base64url'de 43 karakter eder — alt sınırın tam kendisi.
_CODE_VERIFIER_BYTES = 32
_STATE_BYTES = 32
_NONCE_BYTES = 32


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def code_challenge_s256(code_verifier: str) -> str:
    """S256 challenge. `plain` is not implemented: it protects nothing."""

    return _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())


@dataclass(frozen=True)
class OidcConfig:
    """Relying-party configuration. Every field that could be guessed is refused instead."""

    issuer: str
    client_id: str
    redirect_uri: str
    client_secret: str | None = None
    token_endpoint_auth_method: TokenEndpointAuthMethod = "none"
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    # Nokta yolu: Keycloak `realm_access.roles`, Entra ID `roles` ya da `groups`.
    # Tek segmentli yol da geçerlidir.
    roles_claim_path: tuple[str, ...] = ("realm_access", "roles")
    # Tenant iki yoldan gelebilir: doğrulanmış bir claim'den (Entra `tid`) ya da tek
    # kiracılı bir kurulumda sabit değerden. İKİSİ de yoksa fail-closed.
    tenant_claim_path: tuple[str, ...] | None = None
    tenant_id: str | None = None
    subject_claim: str = "sub"
    allowed_algorithms: tuple[str, ...] = ("RS256",)
    # Sınırlı kayma. 60 sn "IdP ile bizim aramızdaki NTP farkı" içindir; saatlerce
    # kayma toleransı süresi dolmuş bir token'ı kabul etmenin başka adıdır.
    max_clock_skew_s: float = 60.0
    discovery_ttl_s: float = 3600.0
    # Bilinmeyen `kid` gördüğümüzde JWKS'i bir kez yeniden çekeriz. Bu aralık, uydurma
    # `kid`li token seli gönderen birinin bizi IdP'ye DDoS aracı yapmasını engeller.
    jwks_min_refetch_interval_s: float = 300.0
    fetch_userinfo: bool = False
    discovery_url: str | None = None
    http_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        for name in ("issuer", "client_id", "redirect_uri"):
            if not getattr(self, name).strip():
                raise OidcConfigurationError("incomplete_oidc_config", detail=f"{name} is required")
        if self.issuer.rstrip("/") != self.issuer:
            raise OidcConfigurationError(
                "issuer_trailing_slash",
                detail="issuer must be written exactly as the IdP emits `iss` (no trailing slash)",
            )
        if "openid" not in self.scopes:
            raise OidcConfigurationError("missing_openid_scope")
        if not self.allowed_algorithms:
            raise OidcConfigurationError("empty_algorithm_allowlist")
        if "none" in {algorithm.casefold() for algorithm in self.allowed_algorithms}:
            raise OidcConfigurationError("unsigned_algorithm_not_allowed")
        if not self.roles_claim_path:
            raise OidcConfigurationError("empty_roles_claim_path")
        needs_secret = self.token_endpoint_auth_method != "none"
        if needs_secret and not self.client_secret:
            raise OidcConfigurationError("missing_client_secret")
        if not needs_secret and self.client_secret:
            raise OidcConfigurationError(
                "unused_client_secret",
                detail="a public client must not be configured with a secret it never sends",
            )
        if self.max_clock_skew_s < 0 or self.discovery_ttl_s < 0 or self.jwks_min_refetch_interval_s < 0:
            raise OidcConfigurationError("negative_interval")

    @property
    def resolved_discovery_url(self) -> str:
        if self.discovery_url:
            return self.discovery_url
        return f"{self.issuer}/.well-known/openid-configuration"


@dataclass(frozen=True)
class AuthorizationRequest:
    """Everything the caller must keep server-side until the IdP redirects back."""

    authorization_url: str
    state: str
    nonce: str
    code_verifier: str


@dataclass(frozen=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None


@dataclass
class _JwksCache:
    key_set: Any = None
    forced_at: float | None = None
    fetches: int = 0


@dataclass
class _MetadataCache:
    metadata: ProviderMetadata | None = None
    fetched_at: float | None = None
    fetches: int = 0


def claim_path_value(claims: Mapping[str, Any], path: Sequence[str]) -> Any:
    """Walk a dotted claim path. A missing or non-mapping segment yields `None`."""

    cursor: Any = claims
    for segment in path:
        if not isinstance(cursor, Mapping) or segment not in cursor:
            return None
        cursor = cursor[segment]
    return cursor


def _as_role_set(value: Any) -> frozenset[str]:
    """Accept the two shapes IdPs actually emit: a list, or a delimited string."""

    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(part for part in value.replace(",", " ").split() if part)
    if isinstance(value, Sequence):
        return frozenset(str(item).strip() for item in value if str(item).strip())
    return frozenset()


class OidcIdentityProvider:
    """Authorization-code + PKCE relying party. Every failure path is fail-closed."""

    def __init__(
        self,
        config: OidcConfig,
        *,
        http_client: Any | None = None,
        clock: WallClock = system_wall_clock,
        token_urlsafe: Any = secrets.token_urlsafe,
    ) -> None:
        self._config = config
        self._client = http_client
        self._owns_client = http_client is None
        self._clock = clock
        self._token_urlsafe = token_urlsafe
        self._jwks = _JwksCache()
        self._metadata = _MetadataCache()

    # ------------------------------------------------------------------ port surface

    @property
    def ready(self) -> bool:
        """`True` once a trusted provider is *configured*.

        ⚠️ Bu "IdP ayakta" DEMEK DEĞİLDİR ve öyle olduğunu iddia etmiyor. `/ready`
        sözleşmesi `api.py`de "güvenilir bir kimlik sağlayıcı bağlandı mı" sorusudur;
        canlı erişilebilirlik ancak ilk login denemesinde ölçülür. Buradan ağ çağrısı
        yapmak readiness probe'unu IdP'ye bağımlı hale getirirdi.
        """

        return bool(self._config.issuer and self._config.client_id and self._config.redirect_uri)

    async def resolve(self, *, tenant_id: str | None, user_id: str | None, roles: str | None) -> Principal:
        """Refuse. OIDC principals come from a validated ID token, never from headers."""

        del tenant_id, user_id, roles
        raise IdentityUnavailable(
            "OIDC identity is established by the login flow and read from the server-side "
            "session; request headers are not a trust boundary here."
        )

    # ------------------------------------------------------------------ flow

    async def begin_authorization(self, *, extra_params: Mapping[str, str] | None = None) -> AuthorizationRequest:
        metadata = await self.metadata()
        state = self._token_urlsafe(_STATE_BYTES)
        nonce = self._token_urlsafe(_NONCE_BYTES)
        code_verifier = self._token_urlsafe(_CODE_VERIFIER_BYTES)
        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "scope": " ".join(self._config.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge_s256(code_verifier),
            "code_challenge_method": "S256",
        }
        if extra_params:
            # Bilinen parametreler üzerine YAZILAMAZ: çağıranın `prompt`/`login_hint`
            # ekleyebilmesi, `redirect_uri`yi değiştirebilmesi demek olmamalı.
            for key, value in extra_params.items():
                if key in params:
                    raise OidcConfigurationError("reserved_authorization_parameter")
                params[key] = value
        separator = "&" if "?" in metadata.authorization_endpoint else "?"
        return AuthorizationRequest(
            authorization_url=f"{metadata.authorization_endpoint}{separator}{urlencode(params)}",
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
        )

    async def complete_authorization(
        self,
        *,
        code: str,
        state: str,
        expected_state: str,
        expected_nonce: str,
        code_verifier: str,
    ) -> Principal:
        """Exchange the code and turn a validated ID token into a `Principal`.

        `state` is compared FIRST and in constant time. It is the only check that can be
        made before spending an authorization code, and it is the one that refuses a
        cross-site login attempt.
        """

        if not expected_state or not constant_time_equals(state, expected_state):
            raise AuthorizationFlowError("state_mismatch")

        tokens = await self._exchange_code(code=code, code_verifier=code_verifier)
        raw_id_token = tokens.get("id_token")
        if not isinstance(raw_id_token, str) or not raw_id_token:
            raise TokenEndpointError("missing_id_token")

        claims = await self.validate_id_token(raw_id_token, expected_nonce=expected_nonce)
        if self._config.fetch_userinfo:
            claims = await self._merge_userinfo(claims, access_token=tokens.get("access_token"))
        return self.principal_from_claims(claims)

    # ------------------------------------------------------------------ validation

    async def validate_id_token(self, raw_id_token: str, *, expected_nonce: str) -> dict[str, Any]:
        jws = import_optional("joserfc.jws")
        jwt = import_optional("joserfc.jwt")
        jose_errors = import_optional("joserfc.errors")

        try:
            header = jws.extract_compact(raw_id_token.encode("ascii")).headers()
        except (ValueError, UnicodeEncodeError, jose_errors.JoseError) as exc:
            raise InvalidIdToken("malformed_id_token") from exc

        # ⭐ Algoritma, ANAHTAR ARANMADAN önce izin listesine karşı sınanır. Sıra önemli:
        # imza koduna hiç girmeden `alg: none` ve HS256-ile-RSA-public-key karışıklığı
        # sınıfı kapanır.
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in self._config.allowed_algorithms:
            raise InvalidIdToken("algorithm_not_allowed")

        kid = header.get("kid")
        key = await self._signing_key(kid if isinstance(kid, str) else None)
        try:
            token = jwt.decode(raw_id_token, key, algorithms=list(self._config.allowed_algorithms))
        except jose_errors.JoseError as exc:
            raise InvalidIdToken("signature_invalid") from exc

        claims = dict(token.claims)
        self._validate_claims(claims, expected_nonce=expected_nonce)
        return claims

    def _validate_claims(self, claims: Mapping[str, Any], *, expected_nonce: str) -> None:
        now = epoch_seconds(self._clock())
        skew = self._config.max_clock_skew_s

        issuer = claims.get("iss")
        if not isinstance(issuer, str) or not constant_time_equals(issuer, self._config.issuer):
            raise InvalidIdToken("issuer_mismatch")

        audience = claims.get("aud")
        audiences = (
            (audience,)
            if isinstance(audience, str)
            else tuple(str(item) for item in audience)
            if isinstance(audience, list)
            else ()
        )
        if self._config.client_id not in audiences:
            raise InvalidIdToken("audience_mismatch")
        # OIDC Core §3.1.3.7: `azp` varsa client_id olmak ZORUNDA; birden çok `aud`
        # varsa `azp` bulunmak zorunda. İkisi de burada.
        authorized_party = claims.get("azp")
        if len(audiences) > 1 and authorized_party is None:
            raise InvalidIdToken("missing_azp")
        if authorized_party is not None and authorized_party != self._config.client_id:
            raise InvalidIdToken("azp_mismatch")

        expires_at = _numeric_date(claims.get("exp"))
        if expires_at is None:
            raise InvalidIdToken("missing_exp")
        if now - skew >= expires_at:
            raise InvalidIdToken("token_expired")

        issued_at = _numeric_date(claims.get("iat"))
        if issued_at is None:
            raise InvalidIdToken("missing_iat")
        if issued_at > now + skew:
            raise InvalidIdToken("issued_in_future")

        not_before = _numeric_date(claims.get("nbf"))
        if not_before is not None and not_before > now + skew:
            raise InvalidIdToken("token_not_yet_valid")

        nonce = claims.get("nonce")
        if not expected_nonce or not isinstance(nonce, str) or not constant_time_equals(nonce, expected_nonce):
            raise InvalidIdToken("nonce_mismatch")

        subject = claims.get(self._config.subject_claim)
        if not isinstance(subject, str) or not subject.strip():
            raise InvalidIdToken("missing_subject")

    def principal_from_claims(self, claims: Mapping[str, Any]) -> Principal:
        subject = claims.get(self._config.subject_claim)
        if not isinstance(subject, str) or not subject.strip():
            raise InvalidIdToken("missing_subject")

        tenant = self._config.tenant_id
        if self._config.tenant_claim_path is not None:
            claimed_tenant = claim_path_value(claims, self._config.tenant_claim_path)
            tenant = str(claimed_tenant).strip() if isinstance(claimed_tenant, str | int) else None
        if not tenant:
            # Kanıt yokluğu izin değildir: tenant çıkarılamıyorsa principal kurulmaz.
            raise InvalidIdToken("missing_tenant")

        roles = _as_role_set(claim_path_value(claims, self._config.roles_claim_path))
        return Principal(tenant_id=tenant, user_id=subject.strip(), roles=roles)

    # ------------------------------------------------------------------ metadata / keys

    async def metadata(self) -> ProviderMetadata:
        now = epoch_seconds(self._clock())
        cached = self._metadata
        if cached.metadata is not None and cached.fetched_at is not None:
            if now - cached.fetched_at < self._config.discovery_ttl_s:
                return cached.metadata

        document = await self._get_json(self._config.resolved_discovery_url, what="discovery")
        advertised = document.get("issuer")
        # Discovery belgesinin kendi `issuer` alanı yapılandırmayla BİREBİR eşleşmeli.
        # Eşleşmezse ya yanlış kiracıya bakıyoruz ya da keşif belgesi bize ait değil;
        # her iki halde de doğrulayacağımız `iss` yanlış olurdu.
        if not isinstance(advertised, str) or not constant_time_equals(advertised, self._config.issuer):
            raise OidcDiscoveryError("discovery_issuer_mismatch")
        try:
            metadata = ProviderMetadata(
                issuer=advertised,
                authorization_endpoint=str(document["authorization_endpoint"]),
                token_endpoint=str(document["token_endpoint"]),
                jwks_uri=str(document["jwks_uri"]),
                userinfo_endpoint=(
                    str(document["userinfo_endpoint"]) if document.get("userinfo_endpoint") else None
                ),
            )
        except KeyError as exc:
            raise OidcDiscoveryError("discovery_missing_endpoint") from exc

        self._metadata = _MetadataCache(metadata=metadata, fetched_at=now, fetches=cached.fetches + 1)
        return metadata

    async def _signing_key(self, kid: str | None) -> Any:
        # İLK YÜKLEME ile YENİLEME ayrı tutulur. Birleşseydi, önbellek boşken gelen ilk
        # bilinmeyen `kid` tek bir çekişle sonuçlanır ve "rotasyonda yeniden çek" yolu
        # hiç koşmadan geçmiş sayılırdı — testi de kandıran tam olarak budur.
        if self._jwks.key_set is None:
            await self._refetch_jwks()
        key = self._lookup_key(kid)
        if key is not None:
            return key
        # Bilinmeyen `kid` = anahtar rotasyonu OLABİLİR. Bir kez yeniden çek; hâlâ yoksa
        # REDDET. "Bulamadım" asla "doğrulamadan kabul et" değildir.
        if await self._refetch_jwks():
            key = self._lookup_key(kid)
        if key is None:
            raise InvalidIdToken("unknown_signing_key")
        return key

    def _lookup_key(self, kid: str | None) -> Any:
        key_set = self._jwks.key_set
        if key_set is None:
            return None
        jose_errors = import_optional("joserfc.errors")
        if kid is None:
            # `KeySet.get_by_kid(None)` rastgele bir anahtar seçer — bu, bir saldırganın
            # `kid`i çıkarıp anahtar seçimini bize bırakması demektir. Tek anahtarlı bir
            # JWKS'te belirsizlik yoktur; birden fazlaysa fail-closed.
            keys = list(getattr(key_set, "keys", []))
            return keys[0] if len(keys) == 1 else None
        try:
            return key_set.get_by_kid(kid)
        except (jose_errors.JoseError, ValueError, KeyError):
            return None

    async def _refetch_jwks(self) -> bool:
        """Fetch (or re-fetch) the JWKS. Returns whether a fetch actually happened."""

        now = epoch_seconds(self._clock())
        if self._jwks.key_set is not None and self._jwks.forced_at is not None:
            if now - self._jwks.forced_at < self._config.jwks_min_refetch_interval_s:
                return False

        metadata = await self.metadata()
        document = await self._get_json(metadata.jwks_uri, what="jwks")
        jwk = import_optional("joserfc.jwk")
        jose_errors = import_optional("joserfc.errors")
        try:
            key_set = jwk.KeySet.import_key_set(document)
        except (jose_errors.JoseError, ValueError, TypeError, KeyError) as exc:
            raise OidcDiscoveryError("malformed_jwks") from exc
        self._jwks = _JwksCache(
            key_set=key_set,
            # `forced_at` yalnız önbellek DOLUYKEN yapılan yenilemeyi sınırlar; ilk
            # yükleme oranı hiç etkilemez, yoksa açılıştan hemen sonraki bir rotasyon
            # doğrulanabilir bir token'ı reddettirirdi.
            forced_at=now if self._jwks.key_set is not None else None,
            fetches=self._jwks.fetches + 1,
        )
        return True

    @property
    def jwks_fetch_count(self) -> int:
        """How many JWKS documents this provider has actually fetched. For tests/ops."""

        return self._jwks.fetches

    # ------------------------------------------------------------------ transport

    def _http(self) -> Any:
        if self._client is None:
            httpx = import_optional("httpx")
            self._client = httpx.AsyncClient(timeout=self._config.http_timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_json(self, url: str, *, what: str) -> dict[str, Any]:
        response = await self._http().get(url, headers={"Accept": "application/json"})
        if response.status_code != 200:
            # ⛔ Gövde ASLA hata metnine girmez: keşif/JWKS cevabı bile bir proxy hata
            # sayfası olabilir ve oraya düşen her şey log'a ve HTTP cevabına akar.
            raise OidcDiscoveryError(f"{what}_http_error", detail=f"status={response.status_code}")
        try:
            document = response.json()
        except ValueError as exc:
            raise OidcDiscoveryError(f"{what}_not_json") from exc
        if not isinstance(document, dict):
            raise OidcDiscoveryError(f"{what}_not_object")
        return document

    async def _exchange_code(self, *, code: str, code_verifier: str) -> dict[str, Any]:
        metadata = await self.metadata()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._config.redirect_uri,
            "client_id": self._config.client_id,
            "code_verifier": code_verifier,
        }
        auth = None
        method = self._config.token_endpoint_auth_method
        if method == "client_secret_post":
            data["client_secret"] = self._config.client_secret or ""
        elif method == "client_secret_basic":
            auth = (self._config.client_id, self._config.client_secret or "")

        response = await self._http().post(
            metadata.token_endpoint,
            data=data,
            auth=auth,
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            # Statü kodundan başka hiçbir şey taşınmaz — token uç noktasının hata gövdesi
            # istek parametrelerini (kodu dahil) yansıtabilir.
            raise TokenEndpointError("token_endpoint_error", detail=f"status={response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TokenEndpointError("token_response_not_json") from exc
        if not isinstance(payload, dict):
            raise TokenEndpointError("token_response_not_object")
        return payload

    async def _merge_userinfo(self, claims: dict[str, Any], *, access_token: Any) -> dict[str, Any]:
        metadata = await self.metadata()
        if metadata.userinfo_endpoint is None:
            raise OidcDiscoveryError("userinfo_endpoint_missing")
        if not isinstance(access_token, str) or not access_token:
            raise TokenEndpointError("missing_access_token")
        response = await self._http().get(
            metadata.userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        if response.status_code != 200:
            raise OidcDiscoveryError("userinfo_http_error", detail=f"status={response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise OidcDiscoveryError("userinfo_not_object")
        subject = claims.get(self._config.subject_claim)
        if payload.get("sub") != subject:
            # Farklı `sub` = başka birinin profili. Birleştirmek kimlik karıştırmaktır.
            raise OidcDiscoveryError("userinfo_subject_mismatch")
        # ⭐ ID token KAZANIR. UserInfo imzalı değildir (bir Bearer token'la çekilen düz
        # JSON'dur); imzalı bir claim'i imzasız bir cevapla EZMEK, doğrulamanın tamamını
        # boşa çıkarır. UserInfo yalnız EKSİK alanları doldurabilir — Entra ID'de grup
        # taşması (`_claim_names`) tam olarak bu durumdur.
        merged = dict(payload)
        merged.update(claims)
        return merged


def _numeric_date(value: Any) -> float | None:
    """RFC 7519 NumericDate. A boolean is not a number here; `True` must not read as 1."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
