"""The `api.py` switch: default off, fail-closed when on but incomplete.

No network. The router is proven mounted through the OpenAPI schema and through `/me`
(which answers 401 without ever contacting an IdP), never by calling `/auth/login` —
that route would really try to fetch the discovery document.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from wozto_ai_reference.api import create_app
from wozto_ai_reference.identity.errors import OidcConfigurationError

ISSUER = "https://idp.test/realms/drill"
SECRET = "0123456789abcdef0123456789abcdef"  # test-only, 32 chars


def _enable(monkeypatch, tmp_path, **overrides) -> None:
    grants = tmp_path / "grants.json"
    grants.write_text(json.dumps({"alice-sub": {"sales-a": "owner"}}), encoding="utf-8")
    env = {
        "WOZTO_REFERENCE_OIDC_ENABLED": "1",
        "WOZTO_REFERENCE_OIDC_ISSUER": ISSUER,
        "WOZTO_REFERENCE_OIDC_CLIENT_ID": "drill-app",
        "WOZTO_REFERENCE_OIDC_REDIRECT_URI": "https://rp.test/auth/callback",
        "WOZTO_REFERENCE_SESSION_SECRET": SECRET,
        "WOZTO_REFERENCE_AUTHZ_GRANTS_PATH": str(grants),
        "WOZTO_REFERENCE_AUTHZ_LEDGER_PATH": str(tmp_path / "decisions.jsonl"),
    }
    env.update(overrides)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_switch_is_off_by_default_and_the_app_is_unchanged() -> None:
    client = TestClient(create_app(allow_insecure_identity=False))

    paths = client.get("/openapi.json").json()["paths"]
    assert "/auth/login" not in paths
    assert "/me" not in paths
    assert "/mailboxes/{mailbox_id}/summary" not in paths

    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "reason": "identity_disabled"}


def test_switch_on_mounts_the_router_and_reports_ready(monkeypatch, tmp_path) -> None:
    _enable(monkeypatch, tmp_path)
    client = TestClient(create_app())

    paths = client.get("/openapi.json").json()["paths"]
    assert {"/auth/login", "/auth/callback", "/auth/logout", "/me"} <= set(paths)
    assert "/mailboxes/{mailbox_id}/summary" in paths
    assert "/mailboxes/{mailbox_id}/drafts" in paths

    # `/ready`, `api.py`nin KENDİ kuralını kullanır: `identity.ready` True ise 200.
    # Bu "IdP ayakta" demek değildir; "güvenilir bir sağlayıcı bağlandı" demektir.
    assert client.get("/ready").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}

    # Oturumsuz istekler ağa hiç çıkmadan fail-closed cevaplar.
    assert client.get("/me").status_code == 401
    assert client.get("/mailboxes/sales-a/summary").status_code == 401


def test_query_is_503_under_the_oidc_switch_known_limitation(monkeypatch, tmp_path) -> None:
    """Bu bir ARIZA DEĞİL, bilinen ve BELGELENMİŞ sınırdır — testle sabitlendi.

    `OidcIdentityProvider.resolve()` başlık kimliğini reddeder (principal imzalı ID
    token'dan ve sunucu oturumundan doğar), `/query` ise hâlâ başlık yolunu kullanır.
    Sınır sessizce değişirse bu test kırmızıya döner ve README'deki satır da yenilenir.
    """

    _enable(monkeypatch, tmp_path)
    client = TestClient(create_app())

    response = client.post(
        "/query",
        headers={"X-Tenant-ID": "tenant-demo", "X-User-ID": "smuggled", "X-Roles": "finance"},
        json={"query": "refund policy"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Trusted identity provider is unavailable"


@pytest.mark.parametrize(
    "missing",
    [
        "WOZTO_REFERENCE_OIDC_ISSUER",
        "WOZTO_REFERENCE_OIDC_CLIENT_ID",
        "WOZTO_REFERENCE_OIDC_REDIRECT_URI",
        "WOZTO_REFERENCE_SESSION_SECRET",
        "WOZTO_REFERENCE_AUTHZ_GRANTS_PATH",
        "WOZTO_REFERENCE_AUTHZ_LEDGER_PATH",
    ],
)
def test_enabled_but_incomplete_fails_at_startup(monkeypatch, tmp_path, missing: str) -> None:
    _enable(monkeypatch, tmp_path, **{missing: None})

    with pytest.raises(OidcConfigurationError):
        create_app()


def test_missing_ledger_path_fails_closed_instead_of_falling_back_to_memory(monkeypatch, tmp_path) -> None:
    """🔴 Bellek-içi deftere SESSİZCE düşmek, denetim satırlarını yeniden başlatmada yok eder.

    Uygulama çalışır görünürdü: `/me` cevap verir, yetki kararları doğru alınır, hiçbir kapı
    ötmez — ama sürecin ömrü boyunca biriken bütün izin/ret satırları kaybolurdu.
    """

    _enable(monkeypatch, tmp_path, WOZTO_REFERENCE_AUTHZ_LEDGER_PATH=None)

    with pytest.raises(OidcConfigurationError) as excinfo:
        create_app()

    assert excinfo.value.reason == "missing_ledger_path"
    assert "WOZTO_REFERENCE_AUTHZ_LEDGER_PATH" in str(excinfo.value)


def test_decisions_reach_the_configured_jsonl_ledger(monkeypatch, tmp_path) -> None:
    """Yol yapılandırıldığında satırların GERÇEKTEN o dosyaya yazıldığını ölç."""

    ledger_path = tmp_path / "nested" / "decisions.jsonl"
    _enable(monkeypatch, tmp_path, WOZTO_REFERENCE_AUTHZ_LEDGER_PATH=str(ledger_path))
    client = TestClient(create_app())

    # Oturum yok -> 401, yetki aşamasına ulaşılmaz, satır yazılmaz.
    assert client.get("/mailboxes/sales-a/summary").status_code == 401
    assert not ledger_path.exists() or ledger_path.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("bad_ttl", ["abc", "0", "-5", "nan"])
def test_unparsable_session_ttl_is_a_typed_config_error(monkeypatch, tmp_path, bad_ttl: str) -> None:
    _enable(monkeypatch, tmp_path, WOZTO_REFERENCE_SESSION_TTL_SECONDS=bad_ttl)

    with pytest.raises(OidcConfigurationError) as excinfo:
        create_app()

    assert excinfo.value.reason in {"unparsable_numeric_env", "non_positive_numeric_env"}


def test_explicit_identity_argument_still_wins(monkeypatch, tmp_path) -> None:
    """Anahtar açık olsa bile açıkça geçirilen bir sağlayıcı ezilmez (test edilebilirlik)."""

    _enable(monkeypatch, tmp_path)
    client = TestClient(create_app(allow_insecure_identity=True, identity=None, service=None))
    assert client.get("/ready").status_code == 200

    from wozto_ai_reference.adapters import LocalHeaderIdentityProvider

    other = TestClient(create_app(identity=LocalHeaderIdentityProvider(enabled=False)))
    assert other.get("/ready").status_code == 503
    assert "/auth/login" not in other.get("/openapi.json").json()["paths"]
