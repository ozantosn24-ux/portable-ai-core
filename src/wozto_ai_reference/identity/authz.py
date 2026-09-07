"""Resource-level authorization: a grant table, four actions, and a ledger row per decision.

## Tek kural, her şeyi belirleyen

**Hedef posta kutusu HER ZAMAN yetki tablosundan, principal üzerinden çözülür — istek
girdisinden ASLA.** İstek bir kutu adı taşıyabilir, ama o ad yalnız bir *arama anahtarıdır*;
kararın ve aşağı akıştaki her işin kullandığı kimlik `grant.mailbox_id`dir. Fark
gözle görünsün diye tipler de böyle adlandırıldı: istekten gelen alan
`MailboxResource.requested_mailbox_id`, karardan çıkan alan `Decision.mailbox_id`.

Bu ayrım kaybolduğunda arıza sessizdir: `sales-a`nın sahibi `/mailboxes/sales-b/...`
çağırır, kod "bu kullanıcının bir yetkisi var" diye bakar, rolü bulur ve İSTEKTEKİ kutuya
uygular. Bütün kapılar yeşil verir.

⚠️ **ÖLÇÜLDÜ — ve bu docstring bir kez YANLIŞ yazıldı.** Burada eskiden *"mutasyon
kontrolü (b) tam olarak bunu yakalar"* yazıyordu. YAKALAMIYORDU: `mailbox_id =
grant.mailbox_id` yerine `mailbox_id = requested` konduğunda suite **tamamen yeşil** kaldı (o günkü
sayı pinlenmedi; suite büyüdü).
Sebep, `InMemoryGrantTable.grant_for`ın birebir sözlük araması olması — o tabloda istenen
dizgi ile `grant.mailbox_id` HER ZAMAN aynıdır, yani iki kaynak ayırt edilemez. Ayrımı
ölçmek için aramayı NORMALLEŞTİREN bir tablo gerekir
(`tests/test_identity_authz.py::NormalisingGrantTable`: `SALES-A` istenir, yetki `sales-a`
döner). Onunla `test_decision_carries_the_grants_mailbox_id_not_the_requested_spelling` ve
`test_denied_action_also_records_the_grants_mailbox_id` mutasyonda kırmızıya döner.
⭐ Ders: **iki tarafı tesadüfen eşit olan bir fixture ile bir değişmezi sınamak, onu
sınamak değildir.**

## Rol -> eylem tablosu

| Rol | read | draft | send | view_summary |
|---|---|---|---|---|
| `owner` | ✓ | ✓ | ✓ | ✓ |
| `delegate` | ✓ | ✓ | ✓ | ✓ |
| `manager_view` | — | — | — | ✓ |

`owner`/`delegate` `view_summary`yi de taşır ve bu bilinçli bir okumadır: `manager_view`
"YALNIZ özet" demektir, tersi değil. Kendi kutusunun özetini göremeyen bir sahip bir
güvenlik özelliği değil, bir arızadır. Tablo bir üst-küme ilişkisidir — `manager_view`in
gördüğü her şeyi `owner` da görür.

## Defter

Her karar — **izin de ret de** — tek bir append-only JSONL satırıdır ve satır
`authorize()` İÇİNDE yazılır. Çağırana bırakılsaydı, defterin en çok gerektiği yol
(erken dönen bir ret) satırsız kalırdı. `llm_gateway.AttemptLedger` ile aynı disiplin:
kapsama kuralının istisnası yoktur.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..domain import DecisionReasonCode, Principal
from ..jsonl_ledger import append_jsonl, read_jsonl
from ._support import WallClock, isoformat_utc, system_wall_clock

MailboxRole = Literal["owner", "delegate", "manager_view"]
MailboxAction = Literal["read", "draft", "send", "view_summary"]

_FULL_ACCESS: frozenset[MailboxAction] = frozenset({"read", "draft", "send", "view_summary"})
_SUMMARY_ONLY: frozenset[MailboxAction] = frozenset({"view_summary"})

ROLE_ACTIONS: Mapping[MailboxRole, frozenset[MailboxAction]] = {
    "owner": _FULL_ACCESS,
    "delegate": _FULL_ACCESS,
    "manager_view": _SUMMARY_ONLY,
}

VALID_ROLES: frozenset[str] = frozenset(ROLE_ACTIONS)
VALID_ACTIONS: frozenset[str] = _FULL_ACCESS

# İstekten gelen dizgiler deftere KIRPILARAK yazılır. Bir çağıran 10 MB'lık bir kutu adı
# göndererek defteri şişiremesin diye; ve kırpma DOĞRULAMA DEĞİL, çünkü doğrulama
# ihlalinde satır düşerdi.
_MAX_LEDGER_FIELD = 256


@dataclass(frozen=True)
class MailboxGrant:
    """One principal's role on one mailbox. The only source of a target mailbox id."""

    principal_id: str
    mailbox_id: str
    role: MailboxRole

    def __post_init__(self) -> None:
        if not self.principal_id.strip() or not self.mailbox_id.strip():
            raise ValueError("grant requires a principal id and a mailbox id")
        if self.role not in VALID_ROLES:
            raise ValueError(f"unknown mailbox role: {self.role!r}")


@dataclass(frozen=True)
class MailboxResource:
    """The mailbox the *request* named. Untrusted until a grant confirms it."""

    requested_mailbox_id: str


class GrantTable(Protocol):
    def grant_for(self, *, principal_id: str, mailbox_id: str) -> MailboxGrant | None: ...

    def grants_of(self, principal_id: str) -> Sequence[MailboxGrant]: ...


class InMemoryGrantTable:
    """Deterministic table for tests, the drill, and any single-node deployment."""

    def __init__(self, grants: Iterable[MailboxGrant] = ()) -> None:
        self._by_key: dict[tuple[str, str], MailboxGrant] = {}
        self._by_principal: dict[str, list[MailboxGrant]] = {}
        for grant in grants:
            self.add(grant)

    def add(self, grant: MailboxGrant) -> None:
        key = (grant.principal_id, grant.mailbox_id)
        if key in self._by_key:
            # İki farklı rol aynı çifte yazılırsa hangisinin kazandığı yükleme sırasına
            # kalır — sessiz bir yetki farkı. Yükleme anında patlaması daha iyidir.
            raise ValueError(f"duplicate grant for {grant.principal_id!r} on {grant.mailbox_id!r}")
        self._by_key[key] = grant
        self._by_principal.setdefault(grant.principal_id, []).append(grant)

    def grant_for(self, *, principal_id: str, mailbox_id: str) -> MailboxGrant | None:
        return self._by_key.get((principal_id, mailbox_id))

    def grants_of(self, principal_id: str) -> Sequence[MailboxGrant]:
        return tuple(self._by_principal.get(principal_id, ()))


def grants_from_mapping(raw: Mapping[str, Mapping[str, str]]) -> InMemoryGrantTable:
    """Build a table from `{principal_id: {mailbox_id: role}}`. Unknown roles are refused."""

    table = InMemoryGrantTable()
    for principal_id, mailboxes in raw.items():
        for mailbox_id, role in mailboxes.items():
            if role not in VALID_ROLES:
                raise ValueError(f"unknown mailbox role: {role!r}")
            table.add(MailboxGrant(principal_id=principal_id, mailbox_id=mailbox_id, role=role))  # type: ignore[arg-type]
    return table


class DecisionRecord(BaseModel):
    """One authorization decision. Written once, never revisited."""

    model_config = ConfigDict(frozen=True)

    # Duvar saati, ISO-8601 UTC. `llm_gateway.AttemptRecord.ts` ile aynı sözleşme.
    ts: str = Field(min_length=1)
    # 🔴 `min_length` YOK — ve bu bilinçli. Bu iki alan İSTEKTEN gelen GÖZLEMLERdir, emir
    # değil. `min_length=1` iken boş bir `requested_mailbox_id` (ya da boş `request_id`)
    # `DecisionRecord`u kurarken ValidationError atıyordu ve satır HİÇ YAZILMIYORDU: yani
    # defterin en çok işe yarayacağı an — bozuk bir istek geldiği an — tek iz bırakmadan
    # geçiyordu. Bir denetim kaydının şeması, kaydedeceği olaydan daha katı olamaz.
    request_id: str = Field(max_length=_MAX_LEDGER_FIELD)
    principal_id: str = Field(min_length=1)
    # Yetkiden ÇÖZÜLEN kutu. Yetki yoksa `None` — ve `None` olması, kararın istekteki
    # değere hiç dokunmadığının kanıtıdır.
    mailbox_id: str | None = None
    # İstekte ADI GEÇEN kutu. Şemada ayrı duruyor çünkü "ne istendi" ile "ne verildi"
    # aynı alana yığılırsa defter, ayırt etmek için var olduğu iki olayı karıştırır.
    # Uzunluk `authorize()` içinde KIRPILIR (doğrulanmaz): burada `max_length` ihlali
    # yine satırı düşürürdü — aynı arıza, ters ucundan.
    requested_mailbox_id: str = Field(max_length=_MAX_LEDGER_FIELD)
    action: str = Field(min_length=1)
    decision: Literal["allow", "deny"]
    reason: DecisionReasonCode


class Decision(BaseModel):
    """The answer plus the ledger row it produced."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: DecisionReasonCode
    principal_id: str
    requested_mailbox_id: str
    mailbox_id: str | None
    action: str
    request_id: str
    role: MailboxRole | None = None


class DecisionLedger(Protocol):
    def append(self, record: DecisionRecord) -> None: ...


class InMemoryDecisionLedger:
    def __init__(self) -> None:
        self.records: list[DecisionRecord] = []

    def append(self, record: DecisionRecord) -> None:
        self.records.append(record)


class JsonlDecisionLedger:
    """Append-only JSONL, one `open()` per row. See `..jsonl_ledger` for why."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: DecisionRecord) -> None:
        append_jsonl(self._path, record.model_dump())

    def read_all(self) -> list[DecisionRecord]:
        return [DecisionRecord.model_validate(row) for row in read_jsonl(self._path)]


def authorize(
    principal: Principal,
    resource: MailboxResource,
    action: MailboxAction,
    *,
    grants: GrantTable,
    ledger: DecisionLedger,
    request_id: str,
    clock: WallClock = system_wall_clock,
) -> Decision:
    """Decide, record, return. Exactly one ledger row per call, allow or deny."""

    principal_id = principal.user_id
    requested = resource.requested_mailbox_id

    def record(
        *,
        allowed: bool,
        reason: str,
        mailbox_id: str | None,
        role: MailboxRole | None = None,
    ) -> Decision:
        ledger.append(
            DecisionRecord(
                ts=isoformat_utc(clock()),
                request_id=request_id[:_MAX_LEDGER_FIELD],
                principal_id=principal_id,
                mailbox_id=mailbox_id,
                requested_mailbox_id=requested[:_MAX_LEDGER_FIELD],
                action=action,
                decision="allow" if allowed else "deny",
                reason=reason,
            )
        )
        return Decision(
            allowed=allowed,
            reason=reason,
            principal_id=principal_id,
            requested_mailbox_id=requested,
            mailbox_id=mailbox_id,
            action=action,
            request_id=request_id,
            role=role,
        )

    if action not in VALID_ACTIONS:
        return record(allowed=False, reason="unknown_action", mailbox_id=None)
    if not requested.strip():
        return record(allowed=False, reason="empty_mailbox_id", mailbox_id=None)

    grant = grants.grant_for(principal_id=principal_id, mailbox_id=requested)
    if grant is None:
        # ⭐ Bu satır, principal'in BAŞKA bir kutuda sahip olmasına BAKMAZ. Yetki
        # kutu başınadır; "bir yerde sahibim" hiçbir yerde yetki değildir.
        return record(allowed=False, reason="no_grant_for_mailbox", mailbox_id=None)

    # ⭐ Buradan sonrası yalnız `grant.mailbox_id` kullanır. `requested` bir daha karara
    # girmez; yalnız deftere "ne istenmişti" olarak yazılır.
    mailbox_id = grant.mailbox_id
    if action not in ROLE_ACTIONS[grant.role]:
        return record(allowed=False, reason="role_forbids_action", mailbox_id=mailbox_id, role=grant.role)
    return record(allowed=True, reason="grant_permits_action", mailbox_id=mailbox_id, role=grant.role)
