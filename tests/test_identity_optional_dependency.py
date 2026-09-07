"""The `auth` extra is genuinely optional — proven, not asserted in prose.

Same discipline as `test_llm_gateway_adapters.py`: `sys.modules[name] = None` makes an
import fail exactly as an uninstalled package would, so this runs identically whether or
not `httpx`/`joserfc` happen to be installed in the environment. Two separate claims:

1. importing `wozto_ai_reference.identity` (and building the pure-Python halves —
   sessions and authorization) needs neither package;
2. when a path that *does* need one is reached, the failure names the extra rather than
   leaking an `ImportError` traceback at the caller.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from wozto_ai_reference.domain import Principal
from wozto_ai_reference.identity import (
    InMemoryDecisionLedger,
    InMemoryGrantTable,
    MailboxGrant,
    MailboxResource,
    authorize,
)
from wozto_ai_reference.identity._support import import_optional
from wozto_ai_reference.identity.errors import IdentityDependencyMissing
from wozto_ai_reference.identity.oidc import OidcConfig, OidcIdentityProvider
from wozto_ai_reference.identity.session import InMemorySessionStore, PendingLogin, SessionManager

SECRET = b"0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("module_name", ["httpx", "joserfc", "joserfc.jws", "joserfc.jwt", "joserfc.jwk"])
def test_missing_dependency_names_the_extra(monkeypatch, module_name: str) -> None:
    monkeypatch.setitem(sys.modules, module_name, None)

    with pytest.raises(IdentityDependencyMissing) as info:
        import_optional(module_name)

    message = str(info.value)
    assert module_name in message
    assert '.[auth]' in message, "mesaj hangi extra'nin kurulacagini SOYLEMELI"
    assert info.value.reason == "auth_extra_not_installed"


def test_provider_construction_needs_nothing_but_the_flow_does(monkeypatch) -> None:
    """Kurulum anında patlamaz; ilk AĞ dokunuşunda, extra'yı adıyla söyleyerek patlar."""

    monkeypatch.setitem(sys.modules, "httpx", None)
    provider = OidcIdentityProvider(
        OidcConfig(
            issuer="https://idp.test/realms/drill",
            client_id="c",
            redirect_uri="https://rp.test/cb",
            tenant_id="t",
        )
    )
    assert provider.ready is True

    with pytest.raises(IdentityDependencyMissing) as info:
        asyncio.run(provider.metadata())

    assert '.[auth]' in str(info.value)


def test_id_token_validation_reports_the_missing_jose_dependency(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "joserfc.jws", None)
    provider = OidcIdentityProvider(
        OidcConfig(
            issuer="https://idp.test/realms/drill",
            client_id="c",
            redirect_uri="https://rp.test/cb",
            tenant_id="t",
        )
    )

    with pytest.raises(IdentityDependencyMissing):
        asyncio.run(provider.validate_id_token("a.b.c", expected_nonce="n"))


def test_sessions_and_authorization_work_without_the_extra(monkeypatch) -> None:
    """Paketin YARISI saf Python'dur ve extra olmadan tam olarak çalışır."""

    for name in ("httpx", "joserfc", "joserfc.jws", "joserfc.jwt", "joserfc.jwk"):
        monkeypatch.setitem(sys.modules, name, None)

    manager = SessionManager(store=InMemorySessionStore(), secret=SECRET)
    principal = Principal(tenant_id="t", user_id="alice-sub", roles=frozenset())
    pending = asyncio.run(manager.begin_login(pending=PendingLogin(state="s", nonce="n", code_verifier="v")))
    record = asyncio.run(manager.complete_login(previous_session_id=pending.session_id, principal=principal))
    assert record.session_id != pending.session_id

    decision = authorize(
        principal,
        MailboxResource(requested_mailbox_id="sales-a"),
        "read",
        grants=InMemoryGrantTable([MailboxGrant(principal_id="alice-sub", mailbox_id="sales-a", role="owner")]),
        ledger=InMemoryDecisionLedger(),
        request_id="req-1",
        clock=lambda: datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )
    assert decision.allowed is True


def test_importing_the_package_pulls_in_neither_dependency() -> None:
    """Ölçüm TEMİZ bir yorumlayıcıda yapılır.

    Bu süreçte `httpx` zaten yüklüdür (test istemcisi onu kullanır), bu yüzden
    `assert "httpx" not in sys.modules` burada hiçbir şey kanıtlamazdı — sahte bir
    yeşil olurdu. Ayrı bir süreç, iddiayı gerçekten ölçen tek yoldur.
    """

    code = (
        "import sys; import wozto_ai_reference.identity as _;"
        "print('httpx' in sys.modules, 'joserfc' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False False", result.stdout


def test_switch_off_does_not_import_the_auth_extra() -> None:
    """Anahtar KAPALIYKEN `api.py` neyi import EDER, neyi ETMEZ — ölçülmüş hâliyle.

    ⚠️ Bu testin var olma sebebi, hem README'de hem `api.py`de yazılı bir cümlenin YANLIŞ
    çıkmasıdır: *"anahtar kapalıyken identity alt paketi import edilmez"*. EDİLİR —
    anahtarı okuyabilmek için `config` yüklenir, o da paketi çeker. Gerçekten korunması
    gereken iddia ÜÇÜNCÜ TARAF bağımlılıkların yüklenmemesidir; ölçtüğümüz o.
    """

    code = (
        "import sys;"
        "from wozto_ai_reference.api import create_app;"
        "create_app(allow_insecure_identity=False);"
        "mods=[m for m in sys.modules if m.startswith('wozto_ai_reference.identity')];"
        "print(len(mods) > 0, 'httpx' in sys.modules, 'joserfc' in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    # identity modülleri YÜKLENİR (True); httpx/joserfc YÜKLENMEZ (False False).
    assert result.stdout.strip() == "True False False", result.stdout
