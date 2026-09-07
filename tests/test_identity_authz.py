"""Resource-level authorization: the grant table decides, and every decision is recorded."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from identity_fake_idp import FrozenClock

from wozto_ai_reference.domain import Principal
from wozto_ai_reference.identity.authz import (
    InMemoryDecisionLedger,
    InMemoryGrantTable,
    JsonlDecisionLedger,
    MailboxGrant,
    MailboxResource,
    authorize,
    grants_from_mapping,
)

ALICE = Principal(tenant_id="tenant-drill", user_id="alice-sub", roles=frozenset({"mailbox-user"}))
BOB = Principal(tenant_id="tenant-drill", user_id="bob-sub", roles=frozenset({"mailbox-user"}))
MIA = Principal(tenant_id="tenant-drill", user_id="mia-sub", roles=frozenset({"mailbox-manager"}))

CLOCK = FrozenClock(datetime(2026, 9, 6, 12, 0, tzinfo=UTC))
WRITE_ACTIONS = ("read", "draft", "send")


def _grants() -> InMemoryGrantTable:
    return InMemoryGrantTable(
        [
            MailboxGrant(principal_id="alice-sub", mailbox_id="sales-a", role="owner"),
            MailboxGrant(principal_id="bob-sub", mailbox_id="sales-b", role="owner"),
            MailboxGrant(principal_id="mia-sub", mailbox_id="sales-a", role="manager_view"),
            MailboxGrant(principal_id="mia-sub", mailbox_id="sales-b", role="manager_view"),
        ]
    )


def _decide(principal, mailbox, action, *, grants=None, ledger=None, request_id="req-1"):
    return authorize(
        principal,
        MailboxResource(requested_mailbox_id=mailbox),
        action,
        grants=grants or _grants(),
        ledger=ledger if ledger is not None else InMemoryDecisionLedger(),
        request_id=request_id,
        clock=CLOCK,
    )


# --------------------------------------------------------------------------- cross-user


@pytest.mark.parametrize("action", WRITE_ACTIONS)
def test_alice_cannot_touch_bobs_mailbox(action) -> None:
    ledger = InMemoryDecisionLedger()

    decision = _decide(ALICE, "sales-b", action, ledger=ledger)

    assert decision.allowed is False
    assert decision.reason == "no_grant_for_mailbox"
    # Karar, İSTENEN kutuya hiç dokunmadı: çözülen kimlik yok.
    assert decision.mailbox_id is None
    assert decision.requested_mailbox_id == "sales-b"
    assert len(ledger.records) == 1
    assert ledger.records[0].decision == "deny"
    assert ledger.records[0].mailbox_id is None
    assert ledger.records[0].requested_mailbox_id == "sales-b"


@pytest.mark.parametrize("action", WRITE_ACTIONS)
def test_bob_cannot_touch_alices_mailbox(action) -> None:
    ledger = InMemoryDecisionLedger()

    decision = _decide(BOB, "sales-a", action, ledger=ledger)

    assert decision.allowed is False
    assert decision.reason == "no_grant_for_mailbox"
    assert len(ledger.records) == 1


@pytest.mark.parametrize("action", WRITE_ACTIONS)
def test_owner_may_act_on_their_own_mailbox(action) -> None:
    """Pozitif kontrol: yukarıdaki retler 'her şeyi reddediyor'dan gelmiyor."""

    decision = _decide(ALICE, "sales-a", action)

    assert decision.allowed is True
    assert decision.reason == "grant_permits_action"
    assert decision.mailbox_id == "sales-a"
    assert decision.role == "owner"


class NormalisingGrantTable:
    """A grant table whose lookup is case-insensitive and returns the CANONICAL mailbox id.

    Why this double exists: `InMemoryGrantTable.grant_for` is an exact dict lookup, so the
    requested id and `grant.mailbox_id` are always the SAME string and the two sources are
    indistinguishable. Under that table, replacing `mailbox_id = grant.mailbox_id` with
    `mailbox_id = requested` changes nothing observable — the mutation survives the entire
    suite (measured at the time: every test stayed green). A real table — a database with a case-insensitive
    collation, a directory, an alias table — does not have that property, and that is
    exactly where taking the id from the request starts propagating the caller's spelling
    instead of the record's.

    Here `grant_for("SALES-A")` resolves to a grant whose `mailbox_id` is `"sales-a"`, so
    the two sources are finally different strings and the tests below can tell them apart.
    """

    def __init__(self, grants) -> None:
        self._by_key = {(g.principal_id, g.mailbox_id.casefold()): g for g in grants}

    def grant_for(self, *, principal_id: str, mailbox_id: str):
        return self._by_key.get((principal_id, mailbox_id.casefold()))

    def grants_of(self, principal_id: str):
        return tuple(g for (p, _), g in self._by_key.items() if p == principal_id)


def test_decision_carries_the_grants_mailbox_id_not_the_requested_spelling() -> None:
    """⭐ Kararın kimliği YETKİDEN gelir, istekten değil — ikisi farklı dizgiyken ölçülür."""

    grants = NormalisingGrantTable([MailboxGrant(principal_id="alice-sub", mailbox_id="sales-a", role="owner")])
    ledger = InMemoryDecisionLedger()

    decision = _decide(ALICE, "SALES-A", "read", grants=grants, ledger=ledger)

    assert decision.allowed is True
    # İstenen yazım adli iz olarak korunur; ÇÖZÜLEN kimlik yetkininkidir.
    assert decision.requested_mailbox_id == "SALES-A"
    assert decision.mailbox_id == "sales-a"
    assert ledger.records[0].mailbox_id == "sales-a"
    assert ledger.records[0].requested_mailbox_id == "SALES-A"


def test_denied_action_also_records_the_grants_mailbox_id() -> None:
    grants = NormalisingGrantTable([MailboxGrant(principal_id="mia-sub", mailbox_id="sales-a", role="manager_view")])
    ledger = InMemoryDecisionLedger()

    decision = _decide(MIA, "Sales-A", "draft", grants=grants, ledger=ledger)

    assert decision.allowed is False
    assert decision.reason == "role_forbids_action"
    assert decision.mailbox_id == "sales-a"
    assert ledger.records[0].mailbox_id == "sales-a"


def test_a_principal_owning_one_mailbox_is_denied_on_a_mailbox_with_no_grant() -> None:
    """Yetki KUTU BAŞINADIR. 'Bir yerde sahibim' hiçbir yerde yetki değildir."""

    ledger = InMemoryDecisionLedger()
    grants = _grants()
    assert grants.grant_for(principal_id="alice-sub", mailbox_id="sales-a") is not None

    decision = _decide(ALICE, "sales-unknown", "read", grants=grants, ledger=ledger)

    assert decision.allowed is False
    assert decision.reason == "no_grant_for_mailbox"
    assert decision.mailbox_id is None
    assert ledger.records[0].requested_mailbox_id == "sales-unknown"


# --------------------------------------------------------------------------- roles


def test_manager_view_gets_summary_on_both_mailboxes() -> None:
    for mailbox in ("sales-a", "sales-b"):
        decision = _decide(MIA, mailbox, "view_summary")
        assert decision.allowed is True
        assert decision.mailbox_id == mailbox
        assert decision.role == "manager_view"


@pytest.mark.parametrize("action", WRITE_ACTIONS)
@pytest.mark.parametrize("mailbox", ["sales-a", "sales-b"])
def test_manager_view_cannot_read_draft_or_send(action, mailbox) -> None:
    ledger = InMemoryDecisionLedger()

    decision = _decide(MIA, mailbox, action, ledger=ledger)

    assert decision.allowed is False
    assert decision.reason == "role_forbids_action"
    # Yetki VAR, eylem yok: bu yüzden çözülmüş kutu kimliği YAZILIR.
    assert decision.mailbox_id == mailbox
    assert len(ledger.records) == 1


def test_owner_and_delegate_are_a_superset_of_manager_view() -> None:
    grants = InMemoryGrantTable(
        [
            MailboxGrant(principal_id="alice-sub", mailbox_id="sales-a", role="owner"),
            MailboxGrant(principal_id="bob-sub", mailbox_id="sales-a", role="delegate"),
        ]
    )
    for principal in (ALICE, BOB):
        for action in ("read", "draft", "send", "view_summary"):
            assert _decide(principal, "sales-a", action, grants=grants).allowed is True


def test_unknown_action_is_denied_and_recorded() -> None:
    ledger = InMemoryDecisionLedger()

    decision = _decide(ALICE, "sales-a", "delete_everything", ledger=ledger)  # type: ignore[arg-type]

    assert decision.allowed is False
    assert decision.reason == "unknown_action"
    assert len(ledger.records) == 1


# --------------------------------------------------------------------------- ledger


def test_allow_and_deny_each_write_exactly_one_row() -> None:
    ledger = InMemoryDecisionLedger()

    _decide(ALICE, "sales-a", "draft", ledger=ledger, request_id="req-allow")
    assert len(ledger.records) == 1

    _decide(ALICE, "sales-b", "draft", ledger=ledger, request_id="req-deny")
    assert len(ledger.records) == 2

    assert [row.decision for row in ledger.records] == ["allow", "deny"]
    assert [row.request_id for row in ledger.records] == ["req-allow", "req-deny"]


@pytest.mark.parametrize(
    ("mailbox", "request_id"),
    [("", "req-1"), ("sales-a", ""), ("", "")],
)
def test_a_malformed_request_still_produces_a_ledger_row(mailbox: str, request_id: str) -> None:
    """🔴 Denetim kaydının şeması, kaydedeceği olaydan daha KATI olamaz.

    `requested_mailbox_id` ve `request_id` `min_length=1` iken boş bir değer
    `DecisionRecord` kurulurken ValidationError atıyor ve satır HİÇ yazılmıyordu — yani
    defterin en çok işe yarayacağı an (bozuk istek) tek iz bırakmadan geçiyordu.
    """

    ledger = InMemoryDecisionLedger()

    # Karar ne çıkarsa çıksın (boş kutu adı -> deny, boş request_id + geçerli kutu ->
    # allow), ölçülen tek şey SATIRIN YAZILDIĞIdır.
    _decide(ALICE, mailbox, "read", ledger=ledger, request_id=request_id)

    assert len(ledger.records) == 1
    assert ledger.records[0].requested_mailbox_id == mailbox
    assert ledger.records[0].request_id == request_id


def test_oversized_request_strings_are_truncated_not_dropped() -> None:
    """Kırpma DOĞRULAMA DEĞİL: `max_length` ihlali yine satırı düşürürdü — aynı arıza."""

    ledger = InMemoryDecisionLedger()
    huge = "x" * 5000

    _decide(ALICE, huge, "read", ledger=ledger, request_id=huge)

    assert len(ledger.records) == 1
    assert len(ledger.records[0].requested_mailbox_id) == 256
    assert len(ledger.records[0].request_id) == 256


def test_row_carries_the_full_contract() -> None:
    ledger = InMemoryDecisionLedger()

    _decide(ALICE, "sales-a", "send", ledger=ledger, request_id="req-x")
    row = ledger.records[0]

    assert row.ts == "2026-09-06T12:00:00Z"  # ISO-8601 UTC, injected wall clock
    assert row.request_id == "req-x"
    assert row.principal_id == "alice-sub"
    assert row.mailbox_id == "sales-a"
    assert row.action == "send"
    assert row.decision == "allow"
    assert row.reason == "grant_permits_action"


def test_jsonl_ledger_is_append_only_across_two_runs(tmp_path) -> None:
    path = tmp_path / "decisions" / "ledger.jsonl"

    first = JsonlDecisionLedger(path)
    _decide(ALICE, "sales-a", "read", ledger=first, request_id="run1-a")
    _decide(ALICE, "sales-b", "read", ledger=first, request_id="run1-b")
    after_first = path.read_bytes()
    assert len(first.read_all()) == 2

    # Yeni bir süreç/nesne aynı dosyayı açar — eski satırlar KORUNMALI.
    second = JsonlDecisionLedger(path)
    _decide(MIA, "sales-a", "view_summary", ledger=second, request_id="run2-a")

    after_second = path.read_bytes()
    assert after_second.startswith(after_first)  # ilk koşunun baytları birebir duruyor
    rows = second.read_all()
    assert len(rows) == 3
    assert [row.request_id for row in rows] == ["run1-a", "run1-b", "run2-a"]


def test_jsonl_rows_are_platform_stable_single_newline(tmp_path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = JsonlDecisionLedger(path)

    _decide(ALICE, "sales-a", "read", ledger=ledger)
    raw = path.read_bytes()

    # `newline=""` olmasaydı Windows'ta "\r\n" yazılırdı ve aynı defter iki makinede
    # farklı baytlar taşırdı.
    assert raw.endswith(b"\n")
    assert b"\r" not in raw
    assert json.loads(raw.decode("utf-8"))["decision"] == "allow"


# --------------------------------------------------------------------------- table hygiene


def test_duplicate_grant_is_refused_at_load_time() -> None:
    table = InMemoryGrantTable([MailboxGrant(principal_id="a", mailbox_id="m", role="owner")])

    with pytest.raises(ValueError, match="duplicate grant"):
        table.add(MailboxGrant(principal_id="a", mailbox_id="m", role="manager_view"))


def test_grants_from_mapping_refuses_an_unknown_role() -> None:
    with pytest.raises(ValueError, match="unknown mailbox role"):
        grants_from_mapping({"alice-sub": {"sales-a": "superuser"}})


def test_grants_of_lists_only_that_principals_grants() -> None:
    grants = _grants()

    assert {grant.mailbox_id for grant in grants.grants_of("mia-sub")} == {"sales-a", "sales-b"}
    assert {grant.mailbox_id for grant in grants.grants_of("alice-sub")} == {"sales-a"}
    assert grants.grants_of("nobody") == ()
