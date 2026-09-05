"""`FailoverRouter.complete` semantics — hermetic, fake clock, no real sleeps."""

from __future__ import annotations

import asyncio
import json

import pytest

from wozto_ai_reference.llm_gateway import (
    SAVINGS_MODE_PROVIDER_ID,
    AllProvidersUnavailable,
    AmbiguousOutcomeError,
    AmbiguousTimeoutError,
    AuthError,
    BadRequestError,
    ChatMessage,
    ChatRequest,
    CircuitBreaker,
    ContentPolicyError,
    InMemoryAttemptLedger,
    JsonlAttemptLedger,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
    RetryPolicy,
    ServerError,
    StaticTemplateSavingsMode,
    UnclassifiedProviderError,
    Usage,
)
from wozto_ai_reference.llm_gateway.providers import Answer, Fail, ScriptedProvider


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SteppingClock:
    """Okundukça ilerleyen MONOTONİK saat, `latency_ms` ölçülebilsin diye.

    `FakeClock` duruyor; onunla her gecikme 0.0 çıkar ve "süre hangi saatten geliyor"
    sorusu sınanamaz. Başlangıç değeri bilerek gerçekçi: monotonik saatin sıfır noktası
    keyfidir ve bu makinede ~229779 saniyedir — `ts` alanına yazılırsa 1970 üretir.
    """

    def __init__(self, start: float = 229_779.27, step: float = 0.25) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class BrokenAdapter:
    """Kendi `try`'ının DIŞINDA patlayan bir adaptör.

    Uydurma bir senaryo değil: iki adaptör de cevabı, SDK çağrısını saran try/except
    bittikten SONRA ayrıştırıyor (`_text_of`, choice birleştirme, `Completion` kurulumu).
    Orada bir blok `content=None` gelirse çıkan şey `TypeError`'dır — taksonomide
    karşılığı olmayan, yönlendiricinin sınıflandıramadığı bir hata.
    """

    provider_id = "broken"
    model = "broken-model"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ChatRequest):
        self.calls += 1
        raise TypeError("'NoneType' object is not iterable")

    async def stream(self, request: ChatRequest):
        self.calls += 1
        raise TypeError("'NoneType' object is not iterable")
        yield  # pragma: no cover - yalnız bunu async generator yapmak için var


class RecordingSleeper:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.calls: list[float] = []
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)


def request(**overrides) -> ChatRequest:
    payload = {
        "messages": (ChatMessage(role="user", content="kargo ne zaman çıkar"),),
        "idempotency_key": "idem-1",
        "request_id": "req-1",
    }
    payload.update(overrides)
    return ChatRequest(**payload)


def build(
    primary,
    secondary,
    *,
    policy=None,
    breakers=None,
    ledger=None,
    clock=None,
    wall_clock=None,
    savings_mode=None,
):
    from wozto_ai_reference.llm_gateway import FailoverRouter

    return FailoverRouter(
        primary,
        secondary,
        policy if policy is not None else RetryPolicy(max_attempts=2, sleep=RecordingSleeper()),
        breakers,
        ledger if ledger is not None else InMemoryAttemptLedger(),
        savings_mode=savings_mode,
        clock=clock,
        wall_clock=wall_clock,
    )


# ------------------------------------------------------------------- retry -> ok


def test_rate_limit_retry_sleeps_exactly_the_retry_after_value():
    sleeper = RecordingSleeper()
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Fail(RateLimitError("429", retry_after_s=2.5)), Answer("ilk sağlayıcı")])
    secondary = ScriptedProvider("secondary", [Answer("kullanılmamalı")])
    router = build(
        primary,
        secondary,
        policy=RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=30.0, sleep=sleeper),
        ledger=ledger,
    )

    result = asyncio.run(router.complete(request()))

    assert sleeper.calls == [2.5]
    assert result.provider_id == "primary"
    assert result.attempts == 2
    assert secondary.calls == 0, "yeniden deneme başarılıysa failover'a HİÇ geçilmemeli"
    assert [record.outcome for record in ledger.records] == ["error", "ok"]


def test_server_error_retries_then_fails_over():
    sleeper = RecordingSleeper()
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Fail(ServerError("500")), Fail(ServerError("500"))])
    secondary = ScriptedProvider("secondary", [Answer("yedekten cevap")])
    router = build(
        primary,
        secondary,
        policy=RetryPolicy(max_attempts=2, base_delay_s=0.25, sleep=sleeper),
        ledger=ledger,
    )

    result = asyncio.run(router.complete(request()))

    assert primary.calls == 2, "politika 2 deneme diyorsa 2 denenmeli"
    assert result.provider_id == "secondary"
    assert result.text == "yedekten cevap"
    assert result.attempts == 3
    assert sleeper.calls == [0.25], "failover'dan ÖNCE bekleme yok; bekleme yalnız aynı sağlayıcı tekrarında"
    assert [record.outcome for record in ledger.records] == ["error", "error", "ok"]


def test_timeout_is_classified_as_retryable_and_fails_over():
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Fail(ProviderTimeoutError("read timeout"))])
    secondary = ScriptedProvider("secondary", [Answer("yedek")])
    router = build(primary, secondary, policy=RetryPolicy(max_attempts=1, sleep=RecordingSleeper()), ledger=ledger)

    result = asyncio.run(router.complete(request()))

    assert result.provider_id == "secondary"
    assert ledger.records[0].error_class == "ProviderTimeoutError"


def test_auth_error_fails_over_immediately_without_retrying():
    """401 iki deneme arasında geçerli hale gelmez: bekleme = boşa gecikme."""
    sleeper = RecordingSleeper()
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Fail(AuthError("401 invalid key")), Answer("ASLA kullanılmamalı")])
    secondary = ScriptedProvider("secondary", [Answer("yedekten cevap")])
    router = build(primary, secondary, policy=RetryPolicy(max_attempts=5, sleep=sleeper), ledger=ledger)

    result = asyncio.run(router.complete(request()))

    assert primary.calls == 1, "aynı sağlayıcıya ikinci kimlik denemesi YOK"
    assert sleeper.calls == [], "AuthError'da geri çekilme beklemesi yapılmamalı"
    assert result.provider_id == "secondary"


def test_bad_request_is_raised_without_failover():
    """İstek kusuru: ikinci sağlayıcı da aynı 400'ü verir, kotayı boşa yakma."""
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Fail(BadRequestError("max_tokens invalid"))])
    secondary = ScriptedProvider("secondary", [Answer("çağrılmamalı")])
    router = build(primary, secondary, ledger=ledger)

    with pytest.raises(BadRequestError):
        asyncio.run(router.complete(request()))

    assert secondary.calls == 0
    assert len(ledger.records) == 1


def test_content_policy_refusal_is_not_shopped_around():
    """Reddedilen bir istemi başka sağlayıcıda 'denemek' politika yıkamasıdır."""
    primary = ScriptedProvider("primary", [Fail(ContentPolicyError("refused"))])
    secondary = ScriptedProvider("secondary", [Answer("çağrılmamalı")])
    router = build(primary, secondary)

    with pytest.raises(ContentPolicyError):
        asyncio.run(router.complete(request()))

    assert secondary.calls == 0


def test_connection_error_retries_the_same_provider_then_fails_over():
    """Davranış tablosundaki 'Bağlantı hatası' satırının SOL sütunu.

    Tablo bu satırı iddia ediyordu ama hiçbir yönlendirici testi sınamıyordu; sınanmamış
    satır belge değil temennidir.
    """
    sleeper = RecordingSleeper()
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider(
        "primary",
        [Fail(ProviderConnectionError("connection reset")), Fail(ProviderConnectionError("connection reset"))],
    )
    secondary = ScriptedProvider("secondary", [Answer("yedekten cevap")])
    router = build(
        primary,
        secondary,
        policy=RetryPolicy(max_attempts=2, base_delay_s=0.25, sleep=sleeper),
        ledger=ledger,
    )

    result = asyncio.run(router.complete(request()))

    assert primary.calls == 2, "bağlantı hatası aynı sağlayıcıda tekrar denenir"
    assert sleeper.calls == [0.25]
    assert result.provider_id == "secondary"
    assert [record.error_class for record in ledger.records] == [
        "ProviderConnectionError",
        "ProviderConnectionError",
        None,
    ]


@pytest.mark.parametrize(
    ("error", "reissued"),
    [
        # pre-send KANITI olanlar: yan etkili istek bile ikinci kez gönderilebilir.
        (ServerError("503 not accepting work", pre_send=True), True),
        (AuthError("401 invalid key"), True),
        # Kanıt YOK ya da istek kusurlu: ikinci gönderim yasak.
        (ProviderConnectionError("connection reset"), False),
        (BadRequestError("max_tokens invalid"), False),
        (ContentPolicyError("refused"), False),
    ],
)
def test_behaviour_table_non_idempotent_column(error, reissued):
    """Tablonun SAĞ sütunu (`idempotent=False`) satır satır sınanır.

    Sol sütunun testi olup sağ sütunun olmaması, tablonun yarısının kanıtsız durması
    demekti — ve sağ sütun tam olarak paranın iki kez harcandığı sütun.
    """
    primary = ScriptedProvider("primary", [Fail(error), Answer("ASLA")])
    secondary = ScriptedProvider("secondary", [Answer("yedekten cevap")])
    router = build(primary, secondary, policy=RetryPolicy(max_attempts=1, sleep=RecordingSleeper()))

    if reissued:
        result = asyncio.run(router.complete(request(idempotent=False)))
        assert result.provider_id == "secondary"
    else:
        with pytest.raises(type(error)):
            asyncio.run(router.complete(request(idempotent=False)))
        assert secondary.calls == 0, "ikinci gönderim = ikinci yan etki"


# ------------------------------------------------- unclassified adapter failures


def test_unmapped_adapter_exception_is_ledgered_and_failed_over():
    """⛔ Sınıflandırılamayan hata SESSİZCE kaçmaz.

    Öncesinde yalnız `GatewayError` yakalanıyordu: adaptörden gelen bir `TypeError`
    defter satırı YAZDIRMADAN, kesiciye DOKUNMADAN ve failover ETMEDEN çağırana
    ulaşıyordu — yani gerçekten yapılmış (ve faturalanmış olabilecek) bir çağrının
    hiçbir izi kalmıyordu.
    """
    sleeper = RecordingSleeper()
    ledger = InMemoryAttemptLedger()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=60.0)
    broken = BrokenAdapter()
    secondary = ScriptedProvider("secondary", [Answer("yedekten cevap")])
    router = build(
        broken,
        secondary,
        policy=RetryPolicy(max_attempts=3, base_delay_s=0.5, sleep=sleeper),
        breakers={"broken": breaker},
        ledger=ledger,
    )

    result = asyncio.run(router.complete(request()))

    assert result.provider_id == "secondary", "idempotent istek yedeğe düşmeli"
    assert broken.calls == 1, "şüpheli ADAPTÖRDÜR; aynı adaptörü tekrar çağırmak aynı kusuru üretir"
    assert sleeper.calls == [], "yeniden deneme yoksa geri çekilme beklemesi de yok"
    assert ledger.records[0].outcome == "error"
    assert ledger.records[0].error_class == "TypeError", "defterde ORİJİNAL tip adı durmalı"
    assert breaker.state == "open", "sağlayıcı arızası sayıldı"


def test_unmapped_adapter_exception_is_ambiguous_for_a_non_idempotent_request():
    """Karşı kontrol: ne olduğunu bilmiyorsak yan etkili istek İKİNCİ KEZ gönderilmez.

    Sınıflandırılamayan bir hata, isteğin sağlayıcıya ULAŞMADIĞININ kanıtı değildir —
    adaptör onu, sağlayıcının çoktan ürettiği (ve faturaladığı) bir cevabı ayrıştırırken
    de atmış olabilir.
    """
    broken = BrokenAdapter()
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(broken, secondary)

    with pytest.raises(AmbiguousOutcomeError) as info:
        asyncio.run(router.complete(request(idempotent=False)))

    assert isinstance(info.value, UnclassifiedProviderError)
    assert isinstance(info.value.__cause__, TypeError), "orijinal hata KAYBOLMAMALI"
    assert secondary.calls == 0


def test_request_defects_do_not_open_the_circuit_of_a_healthy_provider():
    """⛔ Devre kesici ARIZALI SAĞLAYICIYI çitler, ARIZALI İSTEĞİ değil.

    `record_failure()` `reissue_allowed` kapısından ÖNCE koşuyordu; iki reddedilen istem
    sağlıklı bir sağlayıcıyı `open_seconds` boyunca devre dışı bırakıyor ve sıradaki
    ilgisiz çağıranı yedeğe sürüyordu. Sağlayıcının sağlığıyla ilgili hiçbir şey
    olmamışken.
    """
    breaker = CircuitBreaker(failure_threshold=2, open_seconds=60.0)
    primary = ScriptedProvider(
        "primary",
        [Fail(ContentPolicyError("refused")), Fail(ContentPolicyError("refused")), Answer("sağlıklı cevap")],
    )
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(primary, secondary, breakers={"primary": breaker})

    for _ in range(2):
        with pytest.raises(ContentPolicyError):
            asyncio.run(router.complete(request()))

    assert breaker.failures == 0
    assert breaker.state == "closed"

    third = asyncio.run(router.complete(request()))

    assert third.provider_id == "primary", "kesici hiç açılmamalıydı"
    assert third.text == "sağlıklı cevap"
    assert primary.calls == 3
    assert secondary.calls == 0


# ----------------------------------------------------------------- circuit breaker


def test_breaker_opens_and_the_primary_is_not_called_while_open():
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    ledger = InMemoryAttemptLedger()
    breaker = CircuitBreaker(failure_threshold=2, open_seconds=10.0, half_open_max_calls=1, clock=clock)
    primary = ScriptedProvider("primary", [Fail(ServerError("500")), Fail(ServerError("500")), Answer("iyileşti")])
    secondary = ScriptedProvider("secondary", [Answer("yedek-1"), Answer("yedek-2")])
    router = build(
        primary,
        secondary,
        policy=RetryPolicy(max_attempts=2, base_delay_s=0.1, sleep=sleeper),
        breakers={"primary": breaker},
        ledger=ledger,
        clock=clock,
    )

    first = asyncio.run(router.complete(request()))
    assert first.provider_id == "secondary"
    assert primary.calls == 2
    assert breaker.state == "open"

    second = asyncio.run(router.complete(request()))
    assert second.provider_id == "secondary"
    assert primary.calls == 2, "kesici AÇIKKEN birincil HİÇ çağrılmamalı"
    assert any(record.outcome == "skipped_open_circuit" for record in ledger.records)

    # Yarı-açık pencere: birincil tek bir yoklama hakkı alır ve başarırsa kapanır.
    clock.advance(11.0)
    third = asyncio.run(router.complete(request()))
    assert primary.calls == 3
    assert third.provider_id == "primary"
    assert third.text == "iyileşti"
    assert breaker.state == "closed"


def test_both_breakers_open_raises_all_providers_unavailable():
    clock = FakeClock()
    breakers = {
        "primary": CircuitBreaker(failure_threshold=1, open_seconds=60.0, clock=clock),
        "secondary": CircuitBreaker(failure_threshold=1, open_seconds=60.0, clock=clock),
    }
    breakers["primary"].record_failure()
    breakers["secondary"].record_failure()
    primary = ScriptedProvider("primary", [])
    secondary = ScriptedProvider("secondary", [])
    router = build(primary, secondary, breakers=breakers, clock=clock)

    with pytest.raises(AllProvidersUnavailable) as info:
        asyncio.run(router.complete(request()))

    assert info.value.queue_hint is True
    assert primary.calls == 0 and secondary.calls == 0


# ------------------------------------------------------------- ambiguous outcome


def test_non_idempotent_ambiguous_outcome_is_raised_with_a_single_attempt():
    """⛔ ASIL KAPI: yan etkisi olan istek, işlenip işlenmediği BİLİNMEZKEN
    ikinci kez gönderilmez — ne aynı sağlayıcıya, ne yedeğe."""
    sleeper = RecordingSleeper()
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Fail(AmbiguousTimeoutError("gönderim sonrası zaman aşımı")), Answer("x")])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(primary, secondary, policy=RetryPolicy(max_attempts=5, sleep=sleeper), ledger=ledger)

    with pytest.raises(AmbiguousTimeoutError):
        asyncio.run(router.complete(request(idempotent=False)))

    assert primary.calls == 1, "yeniden deneme YOK"
    assert secondary.calls == 0, "failover da YOK — ikisi aynı korumadır"
    assert sleeper.calls == []
    assert len(ledger.records) == 1
    assert ledger.records[0].idempotent is False
    assert ledger.records[0].error_class == "AmbiguousTimeoutError"


def test_idempotent_request_may_retry_the_same_ambiguous_failure():
    """Karşı kontrol: saf üretimde belirsizlik zararsızdır, kapı bunu BLOKLAMAMALI."""
    sleeper = RecordingSleeper()
    primary = ScriptedProvider("primary", [Fail(AmbiguousTimeoutError("timeout")), Answer("ikinci denemede")])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(primary, secondary, policy=RetryPolicy(max_attempts=2, base_delay_s=0.2, sleep=sleeper))

    result = asyncio.run(router.complete(request(idempotent=True)))

    assert result.text == "ikinci denemede"
    assert primary.calls == 2


def test_non_idempotent_pre_send_rate_limit_may_still_be_retried():
    """429 = sunucu işi ALMADI. Kanıt varken yan etkili istek de tekrar edilebilir."""
    sleeper = RecordingSleeper()
    primary = ScriptedProvider("primary", [Fail(RateLimitError("429", retry_after_s=1.0)), Answer("tamam")])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(primary, secondary, policy=RetryPolicy(max_attempts=2, sleep=sleeper))

    result = asyncio.run(router.complete(request(idempotent=False)))

    assert result.text == "tamam"
    assert sleeper.calls == [1.0]


# --------------------------------------------------------- exhaustion / savings


def test_both_providers_failing_raises_with_a_queue_hint():
    primary = ScriptedProvider("primary", [Fail(ServerError("500"))])
    secondary = ScriptedProvider("secondary", [Fail(ServerError("500"))])
    router = build(primary, secondary, policy=RetryPolicy(max_attempts=1, sleep=RecordingSleeper()))

    with pytest.raises(AllProvidersUnavailable) as info:
        asyncio.run(router.complete(request()))

    assert info.value.queue_hint is True


def test_savings_mode_serves_a_template_instead_of_raising():
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Fail(ServerError("500"))])
    secondary = ScriptedProvider("secondary", [Fail(ServerError("500"))])
    router = build(
        primary,
        secondary,
        policy=RetryPolicy(max_attempts=1, sleep=RecordingSleeper()),
        ledger=ledger,
        savings_mode=StaticTemplateSavingsMode(text="Şu anda yanıt üretemiyoruz, isteğiniz sıraya alındı."),
    )

    result = asyncio.run(router.complete(request()))

    assert result.provider_id == SAVINGS_MODE_PROVIDER_ID
    assert result.text.startswith("Şu anda")
    # Sıfır token harcandı ve bu KESİN bilinen bir sayı — 'ölçemedik' ile karışmamalı.
    assert result.usage == Usage(input_tokens=0, output_tokens=0, exact=True)
    assert ledger.records[-1].outcome == "savings_mode"
    # Şablon da bir cevaptır: çağıran onun defter satırlarını da bulabilmeli.
    assert result.request_id == "req-1"


# ------------------------------------------------------------------------ ledger


def test_ledger_line_count_equals_attempt_count():
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Fail(ServerError("500")), Fail(ServerError("500"))])
    secondary = ScriptedProvider("secondary", [Answer("yedek")])
    router = build(
        primary,
        secondary,
        policy=RetryPolicy(max_attempts=2, base_delay_s=0.1, sleep=RecordingSleeper()),
        ledger=ledger,
    )

    result = asyncio.run(router.complete(request()))

    assert result.attempts == 3
    assert len(ledger.records) == 3
    assert len(ledger.provider_attempts()) == 3
    assert [record.attempt for record in ledger.records] == [1, 2, 1]
    assert {record.request_id for record in ledger.records} == {"req-1"}
    assert {record.idempotency_key for record in ledger.records} == {"idem-1"}
    assert all(record.latency_ms >= 0.0 for record in ledger.records)


def test_jsonl_ledger_is_append_only_across_runs(tmp_path):
    """İkinci koşu satır EKLER; ilk koşunun satırlarını YENİDEN YAZMAZ."""
    path = tmp_path / "attempts" / "ledger.jsonl"
    ledger = JsonlAttemptLedger(path)

    first_router = build(
        ScriptedProvider("primary", [Fail(ServerError("500"))]),
        ScriptedProvider("secondary", [Answer("bir")]),
        policy=RetryPolicy(max_attempts=1, sleep=RecordingSleeper()),
        ledger=ledger,
    )
    asyncio.run(first_router.complete(request()))
    after_first = path.read_text(encoding="utf-8").splitlines()

    second_router = build(
        ScriptedProvider("primary", [Answer("iki")]),
        ScriptedProvider("secondary", [Answer("kullanılmadı")]),
        ledger=JsonlAttemptLedger(path),
    )
    asyncio.run(second_router.complete(request(request_id="req-2", idempotency_key="idem-2")))
    after_second = path.read_text(encoding="utf-8").splitlines()

    assert len(after_first) == 2
    assert after_second[: len(after_first)] == after_first, "eski satırlar DEĞİŞMEMELİ"
    assert len(after_second) == 3
    assert json.loads(after_second[-1])["request_id"] == "req-2"
    assert [record.request_id for record in JsonlAttemptLedger(path).read_all()] == ["req-1", "req-1", "req-2"]


def test_ledger_ts_comes_from_the_wall_clock_while_latency_stays_monotonic():
    """⛔ İKİ SAAT AYRI. `ts` takvim zamanıdır, `latency_ms` süredir.

    `ts` enjekte edilen monotonik saatten yazılıyordu; o saatin sıfır noktası keyfidir
    (burada 229779.27) ⇒ defterdeki her satır epoch olarak okunduğunda 1970'e düşüyordu
    ve "bu istek ne zaman gitti" sorusu cevapsız kalıyordu.
    """
    monotonic = SteppingClock(start=229_779.27, step=0.25)
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Answer("cevap")])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(
        primary,
        secondary,
        ledger=ledger,
        clock=monotonic,
        # 1767225600 = 2026-01-01T00:00:00Z
        wall_clock=lambda: 1_767_225_600.0,
    )

    asyncio.run(router.complete(request()))

    record = ledger.records[0]
    assert record.ts == "2026-01-01T00:00:00+00:00", "ISO-8601 UTC dizgisi, monotonik sayı DEĞİL"
    assert record.latency_ms == 250.0, "süre hâlâ MONOTONİK saatten (0.25 sn adım)"


def test_completion_carries_the_request_id_that_keys_its_ledger_rows():
    """`attempts=2` gören çağıran "hangi iki satır?" diye sorabilmeli.

    Defterin anahtarı `request_id`; cevabın kendisi onu taşımıyorsa çağıran, kendi
    isteğinin satırlarını bulamaz — hele id'yi yönlendirici ürettiyse.
    """
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Fail(ServerError("500"))])
    secondary = ScriptedProvider("secondary", [Answer("yedek")])
    router = build(primary, secondary, policy=RetryPolicy(max_attempts=1, sleep=RecordingSleeper()), ledger=ledger)

    result = asyncio.run(router.complete(request(request_id=None)))

    assert result.request_id, "yönlendirici ürettiği id'yi cevaba DAMGALAMALI"
    assert {record.request_id for record in ledger.records} == {result.request_id}
    assert result.attempts == len(ledger.records) == 2
    # ⚠️ `result.request_id` TEK BAŞINA yetmez ve bunu ölçtük: pydantic v2'de
    # `model_copy(update={...})` tanımsız anahtarı da örneğe yazar, yani alan MODELDE
    # HİÇ OLMASA DA öznitelik okunur — ama `model_dump()`a girmez. Cevabı loglayan,
    # kuyruğa koyan, API'den dönen her yol serileştirmeden geçer; asıl iddia budur.
    assert result.model_dump()["request_id"] == result.request_id


def test_jsonl_ledger_writes_lf_only_line_endings(tmp_path):
    """Windows'ta metin modu "\\n"i "\\r\\n" yapar; aynı defter iki platformda farklı
    baytlar taşırdı."""
    path = tmp_path / "ledger.jsonl"
    router = build(
        ScriptedProvider("primary", [Answer("cevap")]),
        ScriptedProvider("secondary", [Answer("ASLA")]),
        ledger=JsonlAttemptLedger(path),
    )

    asyncio.run(router.complete(request()))

    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


def test_exact_usage_from_the_provider_survives_to_the_completion():
    primary = ScriptedProvider("primary", [Answer("cevap", usage=Usage(input_tokens=11, output_tokens=4, exact=True))])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    ledger = InMemoryAttemptLedger()
    router = build(primary, secondary, ledger=ledger)

    result = asyncio.run(router.complete(request()))

    assert result.usage.exact is True
    assert result.usage.total_tokens == 15
    assert ledger.records[0].usage == result.usage


def test_two_providers_may_not_share_a_provider_id():
    with pytest.raises(ValueError, match="distinct provider_id"):
        build(ScriptedProvider("same", []), ScriptedProvider("same", []))
