"""Session rotation, expiry, cookie signing and CSRF."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from identity_fake_idp import FrozenClock

from wozto_ai_reference.domain import Principal
from wozto_ai_reference.identity.errors import SessionError
from wozto_ai_reference.identity.session import (
    InMemorySessionStore,
    PendingLogin,
    SessionManager,
    SignedCookieCodec,
)

SECRET = b"0123456789abcdef0123456789abcdef"  # 32 bytes; test-only, not a credential
PRINCIPAL = Principal(tenant_id="tenant-drill", user_id="alice-sub", roles=frozenset({"mailbox-user"}))
PENDING = PendingLogin(state="s", nonce="n", code_verifier="v")


def _manager(clock: FrozenClock | None = None, **kwargs) -> tuple[SessionManager, InMemorySessionStore]:
    store = InMemorySessionStore()
    manager = SessionManager(
        store=store,
        secret=SECRET,
        clock=clock or FrozenClock(datetime(2026, 9, 6, 12, 0, tzinfo=UTC)),
        **kwargs,
    )
    return manager, store


def test_login_rotates_the_session_id_and_deletes_the_old_record() -> None:
    manager, store = _manager()

    pre = asyncio.run(manager.begin_login(pending=PENDING))
    post = asyncio.run(manager.complete_login(previous_session_id=pre.session_id, principal=PRINCIPAL))

    assert post.session_id != pre.session_id
    assert manager.cookie_value(post) != manager.cookie_value(pre)
    # Fixation savunmasının İKİ yarısı: yeni kimlik VE eski kaydın silinmesi.
    assert asyncio.run(store.load(pre.session_id)) is None
    assert asyncio.run(manager.load(cookie_value=manager.cookie_value(pre))) is None
    assert len(store) == 1


def test_logout_invalidates_server_side_so_the_old_cookie_is_dead() -> None:
    manager, store = _manager()
    record = asyncio.run(manager.complete_login(previous_session_id=None, principal=PRINCIPAL))
    cookie = manager.cookie_value(record)
    assert asyncio.run(manager.load(cookie_value=cookie)) is not None

    asyncio.run(manager.logout(session_id=record.session_id))

    assert asyncio.run(manager.load(cookie_value=cookie)) is None
    assert len(store) == 0


def test_expired_session_is_refused_and_evicted() -> None:
    clock = FrozenClock(datetime(2026, 9, 6, 12, 0, tzinfo=UTC))
    manager, store = _manager(clock, ttl_seconds=60)
    record = asyncio.run(manager.complete_login(previous_session_id=None, principal=PRINCIPAL))
    cookie = manager.cookie_value(record)

    clock.advance(59)
    assert asyncio.run(manager.load(cookie_value=cookie)) is not None

    clock.advance(2)
    assert asyncio.run(manager.load(cookie_value=cookie)) is None
    # Yalnız reddedilmiyor, SİLİNİYOR: yoksa depo süresiz büyür.
    assert len(store) == 0


def test_tampered_cookie_never_reaches_the_store() -> None:
    manager, store = _manager()
    record = asyncio.run(manager.complete_login(previous_session_id=None, principal=PRINCIPAL))
    cookie = manager.cookie_value(record)

    session_id, _, signature = cookie.rpartition(".")
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")

    assert asyncio.run(manager.load(cookie_value=f"{session_id}.{flipped}")) is None
    # İmzasız çıplak kimlik de geçmez — kimliğin kendisini bilmek yetmiyor.
    assert asyncio.run(manager.load(cookie_value=session_id)) is None
    assert asyncio.run(manager.load(cookie_value="")) is None


def test_cookie_carries_only_the_opaque_id_not_the_principal() -> None:
    manager, _ = _manager()
    record = asyncio.run(manager.complete_login(previous_session_id=None, principal=PRINCIPAL))

    cookie = manager.cookie_value(record)

    # Cookie'de ne kullanıcı, ne tenant, ne rol. Otorite sunucuda.
    assert PRINCIPAL.user_id not in cookie
    assert PRINCIPAL.tenant_id not in cookie
    assert "mailbox-user" not in cookie
    assert cookie.startswith(record.session_id + ".")


def test_csrf_token_is_per_session_and_required_exactly() -> None:
    manager, _ = _manager()
    first = asyncio.run(manager.complete_login(previous_session_id=None, principal=PRINCIPAL))
    second = asyncio.run(manager.complete_login(previous_session_id=None, principal=PRINCIPAL))

    assert first.csrf_token != second.csrf_token
    manager.verify_csrf(first, first.csrf_token)  # does not raise

    for wrong in (None, "", "nope", second.csrf_token):
        with pytest.raises(SessionError) as excinfo:
            manager.verify_csrf(first, wrong)
        assert excinfo.value.reason == "csrf_token_invalid"


def test_short_signing_secret_is_refused() -> None:
    with pytest.raises(SessionError) as excinfo:
        SignedCookieCodec(b"too-short")

    assert excinfo.value.reason == "session_secret_too_short"


def test_non_positive_ttl_is_refused() -> None:
    with pytest.raises(SessionError) as excinfo:
        SessionManager(store=InMemorySessionStore(), secret=SECRET, ttl_seconds=0)

    assert excinfo.value.reason == "non_positive_session_ttl"


def test_cookie_attributes_are_httponly_lax_and_secure_by_default() -> None:
    manager, _ = _manager()
    record = asyncio.run(manager.complete_login(previous_session_id=None, principal=PRINCIPAL))

    class _Recorder:
        def __init__(self) -> None:
            self.kwargs: dict = {}

        def set_cookie(self, **kwargs) -> None:
            self.kwargs = kwargs

    recorder = _Recorder()
    manager.attach_cookie(recorder, record)

    assert recorder.kwargs["httponly"] is True
    assert recorder.kwargs["samesite"] == "lax"
    # SameSite=Strict OLMAMALI: IdP'den GET ile dönen callback'te cookie gelmez ve
    # login hiç tamamlanamaz. Lax bilinçli bir seçimdir, gevşeklik değil.
    assert recorder.kwargs["secure"] is True


def test_secure_flag_can_be_lowered_explicitly_for_a_plain_http_drill() -> None:
    manager, _ = _manager(cookie_secure=False)
    record = asyncio.run(manager.complete_login(previous_session_id=None, principal=PRINCIPAL))

    class _Recorder:
        kwargs: dict = {}

        def set_cookie(self, **kwargs) -> None:
            type(self).kwargs = kwargs

    manager.attach_cookie(_Recorder(), record)
    assert _Recorder.kwargs["secure"] is False
