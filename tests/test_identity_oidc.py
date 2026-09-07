"""ID-token validation and the PKCE flow, against an in-process IdP. No network."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from identity_fake_idp import FakeIdp, drill_users

from wozto_ai_reference.identity.errors import (
    AuthorizationFlowError,
    InvalidIdToken,
    OidcConfigurationError,
    OidcDiscoveryError,
)
from wozto_ai_reference.identity.oidc import (
    OidcConfig,
    OidcIdentityProvider,
    claim_path_value,
    code_challenge_s256,
)
from wozto_ai_reference.ports import IdentityUnavailable

REDIRECT_URI = "https://rp.test/auth/callback"


def _idp(**kwargs) -> FakeIdp:
    return FakeIdp(users=drill_users(), redirect_uri=REDIRECT_URI, **kwargs)


def _provider(idp: FakeIdp, **config_overrides) -> OidcIdentityProvider:
    config = OidcConfig(
        issuer=idp.issuer,
        client_id=idp.client_id,
        redirect_uri=REDIRECT_URI,
        **config_overrides,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app), base_url=idp.issuer_base)
    return OidcIdentityProvider(config, http_client=client, clock=idp.clock)


def _authorize(idp: FakeIdp, provider: OidcIdentityProvider, username: str = "alice"):
    """Run the browser half of the flow and return (request, code, state)."""

    async def run():
        request = await provider.begin_authorization(extra_params={"login_as": username})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app)) as browser:
            response = await browser.get(request.authorization_url)
        assert response.status_code == 303
        query = parse_qs(urlparse(response.headers["location"]).query)
        return request, query["code"][0], query["state"][0]

    return asyncio.run(run())


def _login(idp: FakeIdp, provider: OidcIdentityProvider, username: str = "alice", **overrides):
    request, code, state = _authorize(idp, provider, username)
    payload = {
        "code": code,
        "state": state,
        "expected_state": request.state,
        "expected_nonce": request.nonce,
        "code_verifier": request.code_verifier,
    }
    payload.update(overrides)
    return asyncio.run(provider.complete_authorization(**payload))


# --------------------------------------------------------------------------- happy path


def test_login_maps_validated_claims_to_a_principal() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_id="tenant-drill")

    principal = _login(idp, provider, "alice")

    assert principal.user_id == "alice-sub"
    assert principal.tenant_id == "tenant-drill"
    assert principal.roles == frozenset({"mailbox-user"})


def test_authorization_url_carries_pkce_state_and_nonce() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_id="t")

    request = asyncio.run(provider.begin_authorization())
    query = parse_qs(urlparse(request.authorization_url).query)

    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["client_id"] == [idp.client_id]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["state"] == [request.state]
    assert query["nonce"] == [request.nonce]
    # Challenge, doğrulayıcıdan TÜRETİLMİŞ olmalı — sabit ya da ilgisiz bir değer değil.
    assert query["code_challenge"] == [code_challenge_s256(request.code_verifier)]


def test_code_challenge_is_unpadded_base64url_sha256() -> None:
    verifier = "a" * 43
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    assert code_challenge_s256(verifier) == expected
    assert "=" not in code_challenge_s256(verifier)


def test_roles_claim_path_is_configurable_for_a_flat_claim() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_id="t", roles_claim_path=("preferred_username",))

    principal = _login(idp, provider, "mia")

    # Keycloak `realm_access.roles` yerine düz bir claim okundu — yol gerçekten yapılandırılabilir.
    assert principal.roles == frozenset({"mia"})


def test_tenant_can_come_from_a_validated_claim_instead_of_static_config() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_claim_path=("preferred_username",))

    principal = _login(idp, provider, "bob")

    assert principal.tenant_id == "bob"


# --------------------------------------------------------------------------- rejections


def test_wrong_state_is_rejected_before_the_code_is_spent() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_id="t")

    with pytest.raises(AuthorizationFlowError) as excinfo:
        _login(idp, provider, expected_state="a-different-state")

    assert excinfo.value.reason == "state_mismatch"
    # ⭐ Kanıt: token uç noktası HİÇ çağrılmadı. `state` kontrolü kodu harcamadan önce olur.
    assert idp.request_counts.get("token", 0) == 0


def test_wrong_nonce_is_rejected() -> None:
    idp = _idp(nonce_override="nonce-from-another-login")
    provider = _provider(idp, tenant_id="t")

    with pytest.raises(InvalidIdToken) as excinfo:
        _login(idp, provider)

    assert excinfo.value.reason == "nonce_mismatch"


def test_expired_id_token_is_rejected() -> None:
    # Negatif ömür: `exp` daima `iat`ten önce, yani kayma penceresinin de dışında.
    idp = _idp(lifetime_seconds=-3600)
    provider = _provider(idp, tenant_id="t")

    with pytest.raises(InvalidIdToken) as excinfo:
        _login(idp, provider)

    assert excinfo.value.reason == "token_expired"


def test_bounded_skew_accepts_a_token_that_expired_inside_the_window() -> None:
    """Pozitif kontrol: reddin sebebi 'her zaman reddediyor' değil, GERÇEKTEN süre."""

    idp = _idp(lifetime_seconds=10)
    provider = _provider(idp, tenant_id="t", max_clock_skew_s=120)
    request, code, state = _authorize(idp, provider)
    idp.clock.advance(60)  # token süresi doldu, ama 120 sn kayma penceresi içinde

    principal = asyncio.run(
        provider.complete_authorization(
            code=code,
            state=state,
            expected_state=request.state,
            expected_nonce=request.nonce,
            code_verifier=request.code_verifier,
        )
    )
    assert principal.user_id == "alice-sub"


def test_token_expired_beyond_the_skew_window_is_rejected() -> None:
    idp = _idp(lifetime_seconds=10)
    provider = _provider(idp, tenant_id="t", max_clock_skew_s=30)
    request, code, state = _authorize(idp, provider)
    idp.clock.advance(600)

    with pytest.raises(InvalidIdToken) as excinfo:
        asyncio.run(
            provider.complete_authorization(
                code=code,
                state=state,
                expected_state=request.state,
                expected_nonce=request.nonce,
                code_verifier=request.code_verifier,
            )
        )
    assert excinfo.value.reason == "token_expired"


def test_wrong_audience_is_rejected() -> None:
    idp = _idp(audience_override="some-other-client")
    provider = _provider(idp, tenant_id="t")

    with pytest.raises(InvalidIdToken) as excinfo:
        _login(idp, provider)

    assert excinfo.value.reason == "audience_mismatch"


def test_wrong_issuer_is_rejected() -> None:
    idp = _idp(issuer_override="https://evil.test/realms/drill")
    provider = _provider(idp, tenant_id="t")

    with pytest.raises(InvalidIdToken) as excinfo:
        _login(idp, provider)

    assert excinfo.value.reason == "issuer_mismatch"


def test_issued_in_the_future_is_rejected() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_id="t", max_clock_skew_s=5)
    request, code, state = _authorize(idp, provider)
    # Token damgalandı; şimdi RP'nin saatini GERİ al -> `iat` gelecekte kalır.
    idp.clock.moment = datetime(2026, 9, 6, 11, 0, tzinfo=UTC)

    with pytest.raises(InvalidIdToken) as excinfo:
        asyncio.run(
            provider.complete_authorization(
                code=code,
                state=state,
                expected_state=request.state,
                expected_nonce=request.nonce,
                code_verifier=request.code_verifier,
            )
        )
    assert excinfo.value.reason == "issued_in_future"


def test_missing_tenant_fails_closed() -> None:
    idp = _idp()
    # Ne sabit tenant ne de tenant claim'i var.
    provider = _provider(idp, tenant_claim_path=("no_such_claim",))

    with pytest.raises(InvalidIdToken) as excinfo:
        _login(idp, provider)

    assert excinfo.value.reason == "missing_tenant"


# --------------------------------------------------------------------------- JWKS rotation


def test_unknown_kid_triggers_exactly_one_refetch_then_rejects() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_id="t")
    _login(idp, provider)  # önbelleği ısıt: bu ilk YÜKLEME, yenileme değil
    assert idp.request_counts["jwks"] == 1

    idp.sign_with_unpublished_key = True  # JWKS'in içermediği bir `kid`
    with pytest.raises(InvalidIdToken) as excinfo:
        _login(idp, provider)

    assert excinfo.value.reason == "unknown_signing_key"
    # Isıtmadan SONRA tam olarak bir çekiş daha: tek yenileme, ardından ret.
    assert idp.request_counts["jwks"] == 2


def test_rotated_key_is_accepted_after_one_refetch() -> None:
    """Pozitif kontrol: yukarıdaki ret 'yenileme hiç işe yaramıyor'dan GELMİYOR."""

    idp = _idp()
    provider = _provider(idp, tenant_id="t")
    _login(idp, provider)
    assert idp.request_counts["jwks"] == 1

    # IdP anahtarını döndürdü ve yenisini yayımladı; RP'nin önbelleği artık BAYAT.
    idp.sign_with_unpublished_key = True
    idp.publish_rotated_key()
    principal = _login(idp, provider)

    assert principal.user_id == "alice-sub"
    assert idp.request_counts["jwks"] == 2


def test_repeat_unknown_kid_does_not_refetch_again_inside_the_interval() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_id="t", jwks_min_refetch_interval_s=300)
    _login(idp, provider)
    assert idp.request_counts["jwks"] == 1

    idp.sign_with_unpublished_key = True
    for _ in range(3):
        with pytest.raises(InvalidIdToken):
            _login(idp, provider)

    # Üç sahte token, ama yalnız TEK yenileme. Aksi hâlde uydurma `kid` taşıyan bir
    # token seli, bu uç noktayı IdP'ye karşı bir yükseltme aracına çevirirdi.
    assert idp.request_counts["jwks"] == 2


# --------------------------------------------------------------------------- algorithm gate


def test_algorithm_allowlist_is_checked_before_any_key_lookup() -> None:
    """`alg` izin listesinde değilse anahtar HİÇ aranmaz — imza koduna girilmez.

    Ölçüm: sağlayıcı JWKS'i hiç çekmemiş olmalı. Anahtar araması önce yapılsaydı
    `jwks` sayacı artardı ve `alg` kapısı imza yolundan SONRA gelirdi.
    """

    idp = _idp()
    # IdP RS256 imzalar; RP yalnız ES256'ya izin veriyor.
    provider = _provider(idp, tenant_id="t", allowed_algorithms=("ES256",))
    request, code, state = _authorize(idp, provider)
    assert "jwks" not in idp.request_counts

    with pytest.raises(InvalidIdToken) as excinfo:
        asyncio.run(
            provider.complete_authorization(
                code=code,
                state=state,
                expected_state=request.state,
                expected_nonce=request.nonce,
                code_verifier=request.code_verifier,
            )
        )

    assert excinfo.value.reason == "algorithm_not_allowed"
    # ⭐ Asıl ölçüm: token alındı (`token` sayacı arttı) ama JWKS'e HİÇ gidilmedi.
    # Anahtar araması `alg` kapısından önce olsaydı bu sayaç 1 olurdu.
    assert idp.request_counts["token"] == 1
    assert "jwks" not in idp.request_counts


def test_config_refuses_an_unsigned_algorithm() -> None:
    with pytest.raises(OidcConfigurationError) as excinfo:
        OidcConfig(
            issuer="https://i.test",
            client_id="c",
            redirect_uri="https://r.test/cb",
            allowed_algorithms=("none",),
        )

    assert excinfo.value.reason == "unsigned_algorithm_not_allowed"


def test_malformed_id_token_is_rejected_without_a_key_lookup() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_id="t")

    with pytest.raises(InvalidIdToken) as excinfo:
        asyncio.run(provider.validate_id_token("not-a-jwt", expected_nonce="n"))

    assert excinfo.value.reason == "malformed_id_token"


# --------------------------------------------------------------------------- discovery


def test_discovery_issuer_must_match_the_configured_issuer() -> None:
    """Keşif belgesi BULUNUR ama başka bir `issuer` ilan eder -> fail-closed.

    ⚠️ Bu testin ilk hâli bu dala HİÇ ULAŞMIYORDU: yapılandırılan issuer için keşif yolu
    sahte IdP'de yok, 404 dönüyor ve `discovery_http_error` ile bitiyordu — yani ölçülen
    şey "eşleşme kontrolü" değil "sayfa yok"tu. Burada belge yapılandırılan URL'de
    GERÇEKTEN servis edilir ve içinde farklı bir issuer taşır.
    """

    idp = _idp()
    # Yalnız İLAN EDİLEN issuer değişir; keşif yolu aynı kalır, yani belge BULUNUR.
    idp.advertised_issuer_override = "https://idp.test/realms/somewhere-else"
    provider = _provider(idp, tenant_id="t")

    with pytest.raises(OidcDiscoveryError) as excinfo:
        asyncio.run(provider.metadata())

    assert excinfo.value.reason == "discovery_issuer_mismatch"
    # Pozitif kontrol: belge gerçekten getirildi (404'e düşmedik).
    assert idp.request_counts["discovery"] == 1


def test_discovery_at_a_missing_path_is_a_different_failure() -> None:
    """`discovery_http_error` ile `discovery_issuer_mismatch` AYRI arızalardır."""

    idp = _idp()
    config = OidcConfig(issuer="https://idp.test/realms/other", client_id=idp.client_id, redirect_uri=REDIRECT_URI)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app), base_url=idp.issuer_base)
    provider = OidcIdentityProvider(config, http_client=client, clock=idp.clock)

    with pytest.raises(OidcDiscoveryError) as excinfo:
        asyncio.run(provider.metadata())

    assert excinfo.value.reason == "discovery_http_error"


def test_discovery_is_cached_until_the_ttl_expires() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_id="t", discovery_ttl_s=600)

    asyncio.run(provider.metadata())
    asyncio.run(provider.metadata())
    assert idp.request_counts["discovery"] == 1

    idp.clock.advance(601)
    asyncio.run(provider.metadata())
    assert idp.request_counts["discovery"] == 2


# --------------------------------------------------------------------------- port contract


def test_resolve_refuses_header_identity() -> None:
    idp = _idp()
    provider = _provider(idp, tenant_id="t")

    assert provider.ready is True
    with pytest.raises(IdentityUnavailable):
        asyncio.run(provider.resolve(tenant_id="t", user_id="smuggled", roles="admin"))


def test_claim_path_value_walks_and_fails_soft() -> None:
    claims = {"realm_access": {"roles": ["a", "b"]}}

    assert claim_path_value(claims, ("realm_access", "roles")) == ["a", "b"]
    assert claim_path_value(claims, ("realm_access", "missing")) is None
    assert claim_path_value(claims, ("realm_access", "roles", "deeper")) is None
