"""End-to-end: browser redirects, session cookie, CSRF, and the two guarded resources.

The relying party runs under `TestClient`; the IdP runs under a second `TestClient` on
the same in-memory `FakeIdp`. The redirect chain is followed by hand — exactly what a
browser (and the curl script in the live drill) does — so the test exercises the real
`Location` headers rather than calling the handlers directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from identity_fake_idp import FakeIdp, FrozenClock, drill_users

from wozto_ai_reference.identity.authz import InMemoryDecisionLedger, MailboxGrant, grants_from_mapping
from wozto_ai_reference.identity.oidc import OidcConfig, OidcIdentityProvider
from wozto_ai_reference.identity.session import InMemorySessionStore, SessionManager
from wozto_ai_reference.identity.web import IdentityWeb, build_identity_router

REDIRECT_URI = "https://rp.test/auth/callback"
SECRET = b"0123456789abcdef0123456789abcdef"  # test-only

GRANTS = {
    "alice-sub": {"sales-a": "owner"},
    "bob-sub": {"sales-b": "owner"},
    "mia-sub": {"sales-a": "manager_view", "sales-b": "manager_view"},
}


class Harness:
    def __init__(self) -> None:
        self.idp = FakeIdp(users=drill_users(), redirect_uri=REDIRECT_URI)
        self.clock = FrozenClock(datetime(2026, 9, 6, 12, 0, tzinfo=UTC))
        self.ledger = InMemoryDecisionLedger()
        self.store = InMemorySessionStore()
        provider = OidcIdentityProvider(
            OidcConfig(
                issuer=self.idp.issuer,
                client_id=self.idp.client_id,
                redirect_uri=REDIRECT_URI,
                tenant_id="tenant-drill",
            ),
            http_client=httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.idp.app), base_url=self.idp.issuer_base
            ),
            clock=self.idp.clock,
        )
        self.sessions = SessionManager(store=self.store, secret=SECRET, clock=self.clock, cookie_secure=False)
        self.grants = grants_from_mapping(GRANTS)
        self.identity_web = IdentityWeb(
            provider=provider,
            sessions=self.sessions,
            grants=self.grants,
            ledger=self.ledger,
            clock=self.clock,
        )
        self.app = FastAPI()
        self.app.include_router(build_identity_router(self.identity_web))
        app = self.app
        self.client = TestClient(app, base_url="https://rp.test")
        self.idp_client = TestClient(self.idp.app, base_url=self.idp.issuer_base)

    def cookie(self) -> str | None:
        return self.client.cookies.get(self.sessions.cookie_name)

    def start_login(self, username: str) -> tuple[str, str]:
        """Return (code, state) after following the RP -> IdP hop."""

        started = self.client.get("/auth/login", follow_redirects=False)
        assert started.status_code == 303
        authorize_url = started.headers["location"]
        # `login_as`, sahte IdP'nin hangi kullanıcıyı onayladığını seçer.
        separator = "&" if "?" in authorize_url else "?"
        consented = self.idp_client.get(f"{authorize_url}{separator}login_as={username}", follow_redirects=False)
        assert consented.status_code == 303
        query = parse_qs(urlparse(consented.headers["location"]).query)
        return query["code"][0], query["state"][0]

    def login(self, username: str) -> httpx.Response:
        code, state = self.start_login(username)
        return self.client.get(f"/auth/callback?code={code}&state={state}", follow_redirects=False)

    def me(self) -> httpx.Response:
        return self.client.get("/me")


@pytest.fixture
def harness() -> Harness:
    return Harness()


# --------------------------------------------------------------------------- happy path


def test_login_produces_a_session_and_me_shows_the_principal(harness: Harness) -> None:
    response = harness.login("alice")

    assert response.status_code == 303
    assert response.headers["location"] == "/me"

    me = harness.me()
    assert me.status_code == 200
    body = me.json()
    assert body["user_id"] == "alice-sub"
    assert body["tenant_id"] == "tenant-drill"
    assert body["roles"] == ["mailbox-user"]
    assert body["grants"] == [{"mailbox_id": "sales-a", "role": "owner"}]
    assert body["csrf_token"]


def test_me_is_401_without_a_session(harness: Harness) -> None:
    assert harness.me().status_code == 401


def test_session_id_changes_at_login(harness: Harness) -> None:
    harness.client.get("/auth/login", follow_redirects=False)
    pre_login_cookie = harness.cookie()
    assert pre_login_cookie is not None

    harness.client.cookies.clear()
    harness.login("alice")
    post_login_cookie = harness.cookie()

    assert post_login_cookie is not None
    assert post_login_cookie != pre_login_cookie


# --------------------------------------------------------------------------- rejections


def test_callback_with_a_foreign_state_is_rejected(harness: Harness) -> None:
    code, _ = harness.start_login("alice")

    response = harness.client.get(f"/auth/callback?code={code}&state=forged", follow_redirects=False)

    assert response.status_code == 400
    assert response.json()["detail"] == "state_mismatch"
    assert harness.me().status_code == 401


def test_callback_with_a_replayed_nonce_is_rejected(harness: Harness) -> None:
    harness.idp.nonce_override = "nonce-from-another-login"

    response = harness.login("alice")

    assert response.status_code == 400
    assert response.json()["detail"] == "nonce_mismatch"


def test_callback_without_a_login_in_progress_is_rejected(harness: Harness) -> None:
    response = harness.client.get("/auth/callback?code=abc&state=xyz", follow_redirects=False)

    assert response.status_code == 400
    assert response.json()["detail"] == "no_login_in_progress"


def test_a_failed_callback_burns_the_pending_login(harness: Harness) -> None:
    """`state`/`nonce`/verifier TEK KULLANIMLIK: başarısız denemeden sonra tekrar oynatılamaz."""

    code, state = harness.start_login("alice")
    first = harness.client.get(f"/auth/callback?code={code}&state=forged", follow_redirects=False)
    assert first.status_code == 400

    replay = harness.client.get(f"/auth/callback?code={code}&state={state}", follow_redirects=False)
    assert replay.status_code == 400
    assert replay.json()["detail"] == "no_login_in_progress"


# --------------------------------------------------------------------------- logout / CSRF


def test_logout_requires_csrf_and_kills_the_session_server_side(harness: Harness) -> None:
    harness.login("alice")
    csrf = harness.me().json()["csrf_token"]

    assert harness.client.post("/auth/logout").status_code == 403
    assert harness.client.post("/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert harness.me().status_code == 200  # başarısız CSRF oturumu düşürmedi

    stale_cookie = harness.cookie()
    assert harness.client.post("/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204

    # Sunucu tarafı kayıt gitti: ESKİ cookie geri konsa bile 401.
    assert len(harness.store) == 0
    harness.client.cookies.set(harness.sessions.cookie_name, stale_cookie, domain="rp.test")
    assert harness.me().status_code == 401


def test_draft_requires_csrf(harness: Harness) -> None:
    harness.login("alice")

    response = harness.client.post("/mailboxes/sales-a/drafts", json={"subject": "s", "body": "b"})

    assert response.status_code == 403
    assert response.json()["detail"] == "csrf_token_invalid"
    # ⭐ CSRF yetkiden ÖNCE: değerlendirilmemiş bir istek deftere karar YAZMAZ.
    assert harness.ledger.records == []


# --------------------------------------------------------------------------- resources


def test_owner_sees_own_summary_and_is_refused_on_another_mailbox(harness: Harness) -> None:
    harness.login("alice")

    allowed = harness.client.get("/mailboxes/sales-a/summary")
    refused = harness.client.get("/mailboxes/sales-b/summary")

    assert allowed.status_code == 200
    assert allowed.json()["mailbox_id"] == "sales-a"
    assert allowed.json()["role"] == "owner"
    assert refused.status_code == 403
    assert refused.json()["detail"] == "no_grant_for_mailbox"

    assert [(row.decision, row.requested_mailbox_id) for row in harness.ledger.records] == [
        ("allow", "sales-a"),
        ("deny", "sales-b"),
    ]


def test_manager_view_sees_both_summaries_but_cannot_draft(harness: Harness) -> None:
    harness.login("mia")
    csrf = harness.me().json()["csrf_token"]

    assert harness.client.get("/mailboxes/sales-a/summary").status_code == 200
    assert harness.client.get("/mailboxes/sales-b/summary").status_code == 200

    draft = harness.client.post(
        "/mailboxes/sales-a/drafts",
        headers={"X-CSRF-Token": csrf},
        json={"subject": "s", "body": "b"},
    )
    assert draft.status_code == 403
    assert draft.json()["detail"] == "role_forbids_action"


def test_owner_can_create_a_draft_on_their_mailbox(harness: Harness) -> None:
    harness.login("alice")
    csrf = harness.me().json()["csrf_token"]

    response = harness.client.post(
        "/mailboxes/sales-a/drafts",
        headers={"X-CSRF-Token": csrf, "X-Request-ID": "req-draft-1"},
        json={"subject": "teklif", "body": "metin"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["mailbox_id"] == "sales-a"
    assert body["request_id"] == "req-draft-1"
    assert body["persisted"] is False
    assert harness.ledger.records[-1].request_id == "req-draft-1"


class _BrokenLedger:
    """A ledger whose disk is gone. Models the `PermissionError` the drill really hit."""

    def append(self, record) -> None:
        raise PermissionError(13, "Permission denied", "/drill-ledger/decisions.jsonl")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/mailboxes/sales-a/summary", None),  # allow path
        ("get", "/mailboxes/sales-b/summary", None),  # deny path
        ("post", "/mailboxes/sales-a/drafts", {"subject": "s", "body": "b"}),
    ],
)
def test_an_unwritable_ledger_fails_the_request_never_silently_allows(
    harness: Harness, method: str, path: str, body: dict | None
) -> None:
    """⭐ Defter yazılamıyorsa istek BAŞARISIZ olur — kayıtsız bir izin ÜRETİLMEZ.

    Bu sıralama tatbikatta gerçekten ısırdı: `/drill-ledger` kök tarafından sahiplenilmişti
    ve ilk yetki kararı `PermissionError` ile düştü. Doğru davranış buydu — 500, sessiz bir
    200 DEĞİL. `authorize()` satırı karardan ÖNCE yazar; bu test o sırayı sabitler.
    """

    harness.login("alice")
    kwargs: dict = {}
    if body is not None:
        kwargs["json"] = body
        kwargs["headers"] = {"X-CSRF-Token": harness.me().json()["csrf_token"]}
    object.__setattr__(harness.identity_web, "ledger", _BrokenLedger())

    # `raise_server_exceptions=False`: varsayılan TestClient sunucu istisnasını teste geri
    # fırlatır ve gerçek istemcinin GÖRECEĞİ statüyü gizler. Ölçmek istediğimiz tam olarak
    # o statü.
    client = TestClient(harness.app, base_url="https://rp.test", raise_server_exceptions=False)
    client.cookies.update(harness.client.cookies)
    response = getattr(client, method)(path, **kwargs)

    # Ne izin (200/201) ne de "temiz" bir ret (403) — karar KAYDEDİLEMEDİĞİ için verilmez.
    assert response.status_code not in (200, 201, 403)
    assert response.status_code == 500


def test_resource_routes_require_a_session(harness: Harness) -> None:
    assert harness.client.get("/mailboxes/sales-a/summary").status_code == 401
    assert harness.client.post("/mailboxes/sales-a/drafts", json={"subject": "s", "body": "b"}).status_code == 401
    # Kimliksiz istek yetkilendirme aşamasına HİÇ ulaşmaz, dolayısıyla defter boş.
    assert harness.ledger.records == []


def test_draft_body_cannot_smuggle_a_mailbox_id(harness: Harness) -> None:
    harness.login("alice")
    csrf = harness.me().json()["csrf_token"]

    response = harness.client.post(
        "/mailboxes/sales-a/drafts",
        headers={"X-CSRF-Token": csrf},
        json={"subject": "s", "body": "b", "mailbox_id": "sales-b"},
    )

    assert response.status_code == 422  # extra="forbid"


def test_grant_table_change_takes_effect_without_a_new_login(harness: Harness) -> None:
    """Yetki oturumda DONMAZ: karar her istekte tablodan YENİDEN okunur.

    Rol oturum kurulurken kopyalansaydı, bir yetkinin geri alınması ancak kullanıcı
    çıkış yapınca etkili olurdu — iptalin en çok gerektiği anda çalışmayan bir iptal.
    """

    harness.login("alice")
    assert harness.client.get("/mailboxes/sales-b/summary").status_code == 403

    harness.grants.add(MailboxGrant(principal_id="alice-sub", mailbox_id="sales-b", role="manager_view"))

    assert harness.client.get("/mailboxes/sales-b/summary").status_code == 200
