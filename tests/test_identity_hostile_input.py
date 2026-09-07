"""Hostile input must produce a fail-closed REJECTION, never a 500.

The class of bug this file exists for: `hmac.compare_digest` / `secrets.compare_digest`
raise `TypeError: comparing strings with non-ASCII characters is not supported` on a `str`
operand above U+007F. Starlette latin-1-decodes header bytes, so a single `\\xff` in a
cookie reaches the comparison as a non-ASCII `str`. An unhandled TypeError inside an
authentication check is a **500**: the rejection never happens, the reason code is never
written, and the caller learns that the request got as far as the comparison.

Every test here sends raw non-ASCII where a credential is expected and asserts the
documented status and reason — and explicitly asserts the status is not 500.
"""

from __future__ import annotations

import asyncio

import pytest
from identity_fake_idp import FakeIdp, drill_users
from test_identity_web import REDIRECT_URI, Harness

from wozto_ai_reference.identity._support import constant_time_equals
from wozto_ai_reference.identity.errors import SessionError
from wozto_ai_reference.identity.oidc import OidcConfig, OidcIdentityProvider
from wozto_ai_reference.identity.session import InMemorySessionStore, SessionManager, SignedCookieCodec

SECRET = b"0123456789abcdef0123456789abcdef"

# latin-1 çözülmüş bir başlıkta görülebilecek baytlar; hepsi U+007F üstü.
NON_ASCII = "\xff\xfe\xe9"


@pytest.fixture
def harness() -> Harness:
    return Harness()


# --------------------------------------------------------------------------- unit level


def test_constant_time_equals_does_not_raise_on_non_ascii() -> None:
    assert constant_time_equals("abc", "abc") is True
    assert constant_time_equals("abc", NON_ASCII) is False
    assert constant_time_equals(NON_ASCII, NON_ASCII) is True
    # Lone surrogate: `str.encode("utf-8")` bunu da patlatırdı.
    assert constant_time_equals("\ud800", "abc") is False


def test_cookie_codec_rejects_a_non_ascii_signature_without_raising() -> None:
    codec = SignedCookieCodec(SECRET)

    assert codec.unsign(f"abc.{NON_ASCII}") is None
    assert codec.unsign(f"{NON_ASCII}.{NON_ASCII}") is None


def test_verify_csrf_rejects_non_ascii_without_raising() -> None:
    manager = SessionManager(store=InMemorySessionStore(), secret=SECRET)
    record = asyncio.run(
        manager.complete_login(
            previous_session_id=None,
            principal=__import__(
                "wozto_ai_reference.domain", fromlist=["Principal"]
            ).Principal(tenant_id="t", user_id="u"),
        )
    )

    with pytest.raises(SessionError) as excinfo:
        manager.verify_csrf(record, NON_ASCII)
    assert excinfo.value.reason == "csrf_token_invalid"


def test_state_and_nonce_comparison_survive_non_ascii() -> None:
    idp = FakeIdp(users=drill_users(), redirect_uri=REDIRECT_URI)
    provider = OidcIdentityProvider(
        OidcConfig(issuer=idp.issuer, client_id=idp.client_id, redirect_uri=REDIRECT_URI, tenant_id="t"),
        clock=idp.clock,
    )

    from wozto_ai_reference.identity.errors import AuthorizationFlowError

    with pytest.raises(AuthorizationFlowError) as excinfo:
        asyncio.run(
            provider.complete_authorization(
                code="c",
                state=NON_ASCII,
                expected_state="a-real-state",
                expected_nonce="n",
                code_verifier="v",
            )
        )
    assert excinfo.value.reason == "state_mismatch"


# --------------------------------------------------------------------------- HTTP level


# ⚠️ Başlıklar BAYT olarak gönderiliyor, `str` olarak değil. httpx istemcisi bir `str`
# başlık değerini ASCII'ye kodlamaya çalışır ve UnicodeEncodeError verir — yani sunucuya
# HİÇ ulaşamaz. Gerçek bir istemci böyle bir kısıt tanımaz: baytları yollar, Starlette de
# onları latin-1 ile çözüp handler'a non-ASCII bir `str` verir. Testin sunucuyu sınaması
# için istemci tarafını atlamak şart.
RAW_NON_ASCII = b"\xff\xfe\xe9"


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/me", 401), ("/mailboxes/sales-a/summary", 401), ("/auth/callback?code=abc&state=xyz", 400)],
)
def test_non_ascii_cookie_is_rejected_not_a_500(harness: Harness, path: str, expected: int) -> None:
    response = harness.client.get(
        path,
        headers={"Cookie": b"wozto_session=abc." + RAW_NON_ASCII},
        follow_redirects=False,
    )

    assert response.status_code != 500
    assert response.status_code == expected


def test_non_ascii_csrf_header_is_403_not_a_500(harness: Harness) -> None:
    harness.login("alice")

    response = harness.client.post("/auth/logout", headers={"X-CSRF-Token": RAW_NON_ASCII})

    assert response.status_code != 500
    assert response.status_code == 403
    assert response.json()["detail"] == "csrf_token_invalid"


def test_non_ascii_csrf_on_drafts_is_403_not_a_500(harness: Harness) -> None:
    harness.login("alice")

    response = harness.client.post(
        "/mailboxes/sales-a/drafts",
        headers={"X-CSRF-Token": RAW_NON_ASCII},
        json={"subject": "s", "body": "b"},
    )

    assert response.status_code != 500
    assert response.status_code == 403
    # CSRF yetkiden önce: değerlendirilmemiş istek deftere karar yazmaz.
    assert harness.ledger.records == []


def test_non_ascii_state_on_callback_is_400_not_a_500(harness: Harness) -> None:
    code, _ = harness.start_login("alice")

    # `state=stäte` — Starlette bunu percent-decode edip non-ASCII bir str olarak verir.
    response = harness.client.get(
        f"/auth/callback?code={code}&state=st%C3%A4te",
        follow_redirects=False,
    )

    assert response.status_code != 500
    assert response.status_code == 400
    assert response.json()["detail"] == "state_mismatch"


def test_non_ascii_mailbox_id_in_the_path_is_denied_not_a_500(harness: Harness) -> None:
    harness.login("alice")

    response = harness.client.get("/mailboxes/sal%C3%A4s-a/summary")

    assert response.status_code != 500
    assert response.status_code == 403
    assert response.json()["detail"] == "no_grant_for_mailbox"
    # Ret KAYDEDİLDİ: bozuk bir istek de deftere düşer.
    assert len(harness.ledger.records) == 1
    assert harness.ledger.records[0].requested_mailbox_id == "saläs-a"
