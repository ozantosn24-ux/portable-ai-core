"""Append-only record of every provider attempt the router made.

## Why not reuse `TelemetryProvider`

Checked first; it does not fit. `TelemetryProvider.record(TelemetryEvent)` is a
tenant-scoped, trace-keyed event sink whose payload is a flat
`dict[str, str | int | float | bool]` — it models "something notable happened", and an
implementation is free to sample, batch or drop. The attempt ledger answers a
different question: *what did the router try, in what order, and what came back?* It
needs a fixed schema with a nested `Usage`, strict ordering, and durability across a
restart, because it is the artifact you read after a duplicated charge or a surprise
bill. The two can coexist — telemetry for aggregate dashboards, this for reconstruction
— and merging them would force the ledger's guarantees onto every telemetry backend.

## Why append-only

A row is written the moment an attempt *resolves*, and is never revisited. If a later
attempt could rewrite an earlier row, the one thing the ledger exists to prove — that
the request went out twice — becomes the thing it can hide.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .types import Usage

# `abandoned`: sağlayıcı ÇAĞRILDI ama denemeyi ne başarı ne hata bitirdi — tüketici
# akıştan çıktı (istemci koptu, `break`, iptal). Kendi sonucu olmalı: `error` yazmak
# sağlayıcıyı suçlar ve devre kesici istatistiğini kirletir, hiç yazmamak ise gerçekten
# yapılmış (ve faturalanmış olabilecek) bir çağrıyı defterden siler.
AttemptOutcome = Literal["ok", "error", "abandoned", "skipped_open_circuit", "savings_mode"]


class AttemptRecord(BaseModel):
    """One resolved attempt. Every field is answerable at write time or omitted."""

    model_config = ConfigDict(frozen=True)

    # ⚠️ DUVAR SAATİ, ISO-8601 UTC dizgisi — enjekte edilen monotonik saat DEĞİL.
    # Monotonik saatin başlangıcı keyfidir (bu makinede ~229779.27); onu `ts` diye
    # yazmak defterdeki her satırı 1970'e düşürür ve defterin en temel işini —
    # "bu istek NE ZAMAN gitti" — imkânsız kılar. Süre ölçümü hâlâ monotonik saatten
    # gelir ve `latency_ms` alanında durur; iki saatin işi ayrıdır.
    ts: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    idempotent: bool
    provider_id: str = Field(min_length=1)
    # `skipped_open_circuit` satırlarında 0: sağlayıcı HİÇ çağrılmadı, yani bir
    # "deneme numarası" yoktur. Bu satırlar denemeleri SAYARKEN hariç tutulur ama
    # yazılır — kesicinin ısırdığını başka hiçbir kayıt göstermez.
    attempt: int = Field(ge=0)
    outcome: AttemptOutcome
    error_class: str | None = None
    latency_ms: float = Field(ge=0.0)
    usage: Usage | None = None
    model: str | None = None
    # Akış yeniden başlatıldığında atılan karakter sayısı. Buffered modda tüketici bunu
    # HİÇ görmez (kısmi çıktı ona ulaşmadı) — kaydın tek yeri burasıdır.
    discarded_chars: int | None = None
    # Yalnız `abandoned` satırlarında: tüketici çekilene kadar sağlayıcının ÜRETTİĞİ
    # karakter sayısı (buffered olmayan modda bunlar tüketiciye ULAŞMIŞTIR). Ayrı alan,
    # çünkü `discarded_chars` "failover yüzünden çöpe giden metin" demektir ve iki
    # sayıyı tek alana yığmak defterin tam da ayırt etmesi gereken iki olayı karıştırır.
    delivered_chars: int | None = None


class AttemptLedger(Protocol):
    def append(self, record: AttemptRecord) -> None: ...


class InMemoryAttemptLedger:
    """Ledger for tests and local runs. Holds records in call order."""

    def __init__(self) -> None:
        self.records: list[AttemptRecord] = []

    def append(self, record: AttemptRecord) -> None:
        self.records.append(record)

    def provider_attempts(self) -> list[AttemptRecord]:
        """Rows that represent an actual call to a provider."""

        return [record for record in self.records if record.outcome != "skipped_open_circuit"]


class JsonlAttemptLedger:
    """One JSON object per line, opened in append mode for every write.

    Re-opening per record rather than holding a handle is deliberate: the file stays
    readable and rotatable by other tooling while the process runs, and a crash cannot
    lose a record that a buffered handle had not flushed. The cost is one `open()` per
    attempt, which is nothing next to a model call.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: AttemptRecord) -> None:
        line = json.dumps(record.model_dump(), ensure_ascii=False, sort_keys=True)
        # `newline=""` — Windows'ta metin modu "\n"i "\r\n"e çevirir ve JSONL dosyası
        # platforma göre farklı baytlar taşır; aynı defteri iki makinede karşılaştıran
        # (ya da satır uzunluğuna/hash'ine bakan) her araç bunu fark eder. Satır sonunu
        # burada biz yazıyoruz, işletim sistemi değil.
        with self._path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(f"{line}\n")

    def read_all(self) -> list[AttemptRecord]:
        if not self._path.exists():
            return []
        with self._path.open(encoding="utf-8") as handle:
            return [AttemptRecord.model_validate(json.loads(line)) for line in handle if line.strip()]
