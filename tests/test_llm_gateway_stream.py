"""Stream semantics: restart, never splice, and the buffered escape hatch.

The consumer tests below are the point of this file. It is easy to write a router that
emits the right events and still ships a bug, because "the events are correct" and "a
consumer that reads the events ends up with correct text" are different claims. Both
consumer shapes are simulated here against the same event stream.
"""

from __future__ import annotations

import asyncio

import pytest

from wozto_ai_reference.llm_gateway import (
    AllProvidersUnavailable,
    AmbiguousTimeoutError,
    AuthError,
    ChatMessage,
    ChatRequest,
    FailoverRouter,
    InMemoryAttemptLedger,
    RetryPolicy,
    ServerError,
    StaticTemplateSavingsMode,
    StreamEnd,
    StreamRestarted,
    TextDelta,
    Usage,
)
from wozto_ai_reference.llm_gateway.providers import Answer, Fail, PartialThenFail, ScriptedProvider

PRIMARY_CHUNKS = ("Kargolar her ", "sabah 09:00'da ")
SECONDARY_TEXT = "Kargolar hafta içi her sabah 09:00'da çıkar."


class RecordingSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def request(**overrides) -> ChatRequest:
    payload = {
        "messages": (ChatMessage(role="user", content="kargo saati"),),
        "idempotency_key": "idem-s1",
        "request_id": "req-s1",
    }
    payload.update(overrides)
    return ChatRequest(**payload)


def build(primary, secondary, *, ledger=None, savings_mode=None, max_attempts=1):
    return FailoverRouter(
        primary,
        secondary,
        RetryPolicy(max_attempts=max_attempts, base_delay_s=0.1, sleep=RecordingSleeper()),
        None,
        ledger if ledger is not None else InMemoryAttemptLedger(),
        savings_mode=savings_mode,
    )


async def collect(router, **kwargs) -> list:
    return [event async for event in router.stream(request(**kwargs.pop("request_kwargs", {})), **kwargs)]


def partial_then_secondary(ledger=None):
    primary = ScriptedProvider("primary", [PartialThenFail(PRIMARY_CHUNKS, ServerError("500 mid-stream"))])
    secondary = ScriptedProvider("secondary", [Answer(SECONDARY_TEXT)])
    return primary, secondary, build(primary, secondary, ledger=ledger)


# ----------------------------------------------------------- the no-splice rule


def test_restart_is_emitted_before_any_secondary_delta():
    _, _, router = partial_then_secondary()

    events = asyncio.run(collect(router))

    kinds = [type(event).__name__ for event in events]
    restart_at = kinds.index("StreamRestarted")
    first_secondary_at = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, TextDelta) and event.provider_id == "secondary"
    )
    assert restart_at < first_secondary_at, "yedek sağlayıcının İLK delta'sı restart olayından ÖNCE gitmiş"

    restart = events[restart_at]
    assert restart.from_provider == "primary"
    assert restart.to_provider == "secondary"
    assert restart.discarded_chars == len("".join(PRIMARY_CHUNKS))


def test_final_text_is_exactly_one_providers_output():
    _, _, router = partial_then_secondary()

    events = asyncio.run(collect(router))
    end = events[-1]

    assert isinstance(end, StreamEnd)
    assert end.completion.text == SECONDARY_TEXT
    assert end.completion.provider_id == "secondary"
    assert "".join(PRIMARY_CHUNKS) not in end.completion.text


def test_a_naive_concatenating_consumer_is_wrong_and_a_resetting_one_is_right():
    """Aynı olay akışı, iki tüketici. Fark tam olarak StreamRestarted'a uymaktır."""
    _, _, router = partial_then_secondary()
    events = asyncio.run(collect(router))
    end = events[-1]

    naive = "".join(event.text for event in events if isinstance(event, TextDelta))
    resetting = ""
    for event in events:
        if isinstance(event, StreamRestarted):
            resetting = ""
        elif isinstance(event, TextDelta):
            resetting += event.text

    assert naive != end.completion.text, "birleştiren tüketici hiçbir modelin yazmadığı bir metin üretir"
    assert naive.startswith("Kargolar her sabah 09:00'da Kargolar hafta içi"), naive
    assert resetting == end.completion.text
    assert resetting == SECONDARY_TEXT


def test_failure_before_the_first_delta_fails_over_transparently():
    """Hiç metin çıkmadıysa tüketicinin atacağı bir şey yok ⇒ restart olayı da yok."""
    primary = ScriptedProvider("primary", [Fail(AuthError("401"))])
    secondary = ScriptedProvider("secondary", [Answer(SECONDARY_TEXT)])
    router = build(primary, secondary)

    events = asyncio.run(collect(router))

    assert not any(isinstance(event, StreamRestarted) for event in events)
    assert [event.provider_id for event in events if isinstance(event, TextDelta)] == ["secondary"]
    assert events[-1].completion.text == SECONDARY_TEXT


def test_partial_failure_does_not_retry_the_same_provider():
    """Metin çıktıktan sonraki hata 'yeniden deneme' değil RESTART'tır."""
    sleeper = RecordingSleeper()
    primary = ScriptedProvider("primary", [PartialThenFail(PRIMARY_CHUNKS, ServerError("500")), Answer("ASLA")])
    secondary = ScriptedProvider("secondary", [Answer(SECONDARY_TEXT)])
    router = FailoverRouter(
        primary,
        secondary,
        RetryPolicy(max_attempts=5, base_delay_s=0.1, sleep=sleeper),
        None,
        InMemoryAttemptLedger(),
    )

    events = asyncio.run(collect(router))

    assert primary.calls == 1
    assert sleeper.calls == []
    assert events[-1].completion.provider_id == "secondary"


def test_clean_failure_before_output_still_uses_the_retry_budget():
    """Karşı kontrol: hiç delta üretmeden düşen akış AYNI sağlayıcıda tekrar denenir."""
    sleeper = RecordingSleeper()
    primary = ScriptedProvider("primary", [Fail(ServerError("500")), Answer("ikinci denemede primary")])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = FailoverRouter(
        primary,
        secondary,
        RetryPolicy(max_attempts=2, base_delay_s=0.3, sleep=sleeper),
        None,
        InMemoryAttemptLedger(),
    )

    events = asyncio.run(collect(router))

    assert primary.calls == 2
    assert sleeper.calls == [0.3]
    assert secondary.calls == 0
    assert events[-1].completion.text == "ikinci denemede primary"


# ------------------------------------------------------------------- buffered


def test_buffered_mode_hides_partial_output_entirely():
    ledger = InMemoryAttemptLedger()
    _, _, router = partial_then_secondary(ledger)

    events = asyncio.run(collect(router, buffered=True))

    assert not any(isinstance(event, StreamRestarted) for event in events), (
        "buffered modda tüketici kısmi çıktıyı HİÇ görmedi ⇒ atmasını isteyecek bir olay da olmamalı"
    )
    assert {event.provider_id for event in events if isinstance(event, TextDelta)} == {"secondary"}
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == SECONDARY_TEXT
    assert events[-1].completion.text == SECONDARY_TEXT


def test_buffered_discard_is_still_recorded_in_the_ledger():
    """Tüketici görmedi diye olay KAYBOLMAZ; tek kaydı defterdir."""
    ledger = InMemoryAttemptLedger()
    _, _, router = partial_then_secondary(ledger)

    asyncio.run(collect(router, buffered=True))

    discarded = [record.discarded_chars for record in ledger.records if record.discarded_chars]
    assert discarded == [len("".join(PRIMARY_CHUNKS))]


def test_unbuffered_mode_emits_partial_output_as_it_arrives():
    """Karşı kontrol: buffered olmayan mod gerçekten akıtıyor mu?"""
    _, _, router = partial_then_secondary()

    events = asyncio.run(collect(router))

    assert [event.text for event in events if isinstance(event, TextDelta)][: len(PRIMARY_CHUNKS)] == list(
        PRIMARY_CHUNKS
    )


def test_buffered_mode_retries_the_same_provider_after_a_mid_stream_failure():
    """Buffered modda yeniden deneme bütçesi harcanmadan ATLANIYORDU.

    Kural "metin çıktıktan sonra aynı sağlayıcıya tekrar yok" idi ve gerekçesi
    tüketicinin kısmi metni GÖRMÜŞ olmasıydı. Buffered modda tüketici hiçbir şey görmez
    ⇒ gerekçe düşer: iç tampon atılır ve aynı sağlayıcı temiz bir istekle tekrar
    denenir. Aksi hâlde birincil, ilk mikro kesintide gereksiz yere terk ediliyordu.
    """
    sleeper = RecordingSleeper()
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider(
        "primary",
        [PartialThenFail(PRIMARY_CHUNKS, ServerError("500 mid-stream")), Answer("primary ikinci denemede")],
    )
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = FailoverRouter(
        primary,
        secondary,
        RetryPolicy(max_attempts=2, base_delay_s=0.3, sleep=sleeper),
        None,
        ledger,
    )

    events = asyncio.run(collect(router, buffered=True))

    assert primary.calls == 2, "buffered modda aynı sağlayıcı yeniden denenmeli"
    assert secondary.calls == 0
    assert sleeper.calls == [0.3]
    assert not any(isinstance(event, StreamRestarted) for event in events), (
        "tüketici kısmi çıktıyı görmedi ⇒ atmasını isteyecek bir olay da yok"
    )
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "primary ikinci denemede", (
        "ilk denemenin iç tamponu ATILMALI, ikincinin metnine eklenmemeli"
    )
    assert events[-1].completion.provider_id == "primary"
    assert [record.outcome for record in ledger.records] == ["error", "ok"]


# ------------------------------------------------------ abandoned by the consumer


def test_a_consumer_that_walks_away_still_leaves_one_ledger_row():
    """⛔ Sağlayıcı ÇAĞRILDIYSA defterde satırı vardır — tüketici gitse bile.

    İstemci koptuğunda (`break`, iptal) üretece `GeneratorExit` atılır: ne `ok` ne
    `error` yolu koşar. Öncesinde bu, gerçekten yapılmış ve faturalanmış olabilecek bir
    çağrının defterde HİÇ görünmemesi demekti — defterin varlık sebebini ortadan
    kaldıran tek delik.
    """
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [PartialThenFail(PRIMARY_CHUNKS, ServerError("500"))])
    secondary = ScriptedProvider("secondary", [Answer(SECONDARY_TEXT)])
    router = build(primary, secondary, ledger=ledger)

    async def read_one_delta_then_leave() -> None:
        stream = router.stream(request())
        try:
            async for event in stream:
                if isinstance(event, TextDelta):
                    break
        finally:
            # Gerçek tüketici `contextlib.aclosing` kullanır; burada açıkça kapatmak
            # sonlandırmayı DETERMİNİSTİK yapar. Aksi hâlde async generator'ın
            # finalizasyonu çöp toplayıcıya kalır ve test bayrak sallar.
            await stream.aclose()

    asyncio.run(read_one_delta_then_leave())

    assert len(ledger.records) == 1
    row = ledger.records[0]
    assert row.outcome == "abandoned"
    assert row.provider_id == "primary"
    assert row.attempt == 1
    assert row.request_id == "req-s1"
    assert row.idempotency_key == "idem-s1"
    assert row.delivered_chars == len(PRIMARY_CHUNKS[0]), "tüketiciye ulaşmış karakter sayısı"
    assert row.error_class is None, "bu bir sağlayıcı arızası DEĞİL"
    assert row.latency_ms >= 0.0
    assert secondary.calls == 0, "tüketici gitti; failover diye bir şey yok"


def test_a_cancelled_consumer_task_also_leaves_an_abandoned_row():
    """Üretimdeki asıl şekil: istemci koptu ⇒ tüketici GÖREVİ iptal edilir.

    `break` ile aynı deliğin ikinci kapısı ve mekanizması FARKLI (`CancelledError`, bir
    `BaseException`). Testin `break` sürümünü geçip bunun kalması mümkün olmasın diye
    ayrıca sınanıyor.
    """
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [PartialThenFail(PRIMARY_CHUNKS, ServerError("500"))])
    secondary = ScriptedProvider("secondary", [Answer(SECONDARY_TEXT)])
    router = build(primary, secondary, ledger=ledger)

    async def cancel_mid_stream() -> None:
        first_delta_seen = asyncio.Event()

        async def consume() -> None:
            async for event in router.stream(request()):
                if isinstance(event, TextDelta):
                    first_delta_seen.set()
                    await asyncio.sleep(3600)  # istemci burada asılı kaldı

        task = asyncio.create_task(consume())
        await first_delta_seen.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_mid_stream())

    assert [record.outcome for record in ledger.records] == ["abandoned"]
    assert ledger.records[0].delivered_chars == len(PRIMARY_CHUNKS[0])


def test_a_resolved_attempt_the_consumer_leaves_early_is_not_marked_abandoned():
    """Karşı kontrol: `ok` satırı yazıldıktan SONRA çekilen tüketici ikinci satır üretmez.

    Deneme çözülmüştü; üstüne `abandoned` yazmak aynı çağrıyı defterde iki kez
    göstermek olurdu.
    """
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Answer(SECONDARY_TEXT)])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(primary, secondary, ledger=ledger)

    async def leave_during_the_buffered_replay() -> None:
        stream = router.stream(request(), buffered=True)
        try:
            async for event in stream:
                if isinstance(event, TextDelta):
                    break
        finally:
            await stream.aclose()

    asyncio.run(leave_during_the_buffered_replay())

    assert [record.outcome for record in ledger.records] == ["ok"]


class BrokenStreamAdapter:
    """İlk delta'dan SONRA taksonomi dışı bir hata atan adaptör.

    Gerçek desen: akış gövdesi SDK'nın try/except'inin dışında ayrıştırılır; bir parça
    beklenmedik şekil taşırsa çıkan şey `TypeError`'dır.
    """

    provider_id = "broken"
    model = "broken-model"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request):  # pragma: no cover - bu test yalnız stream kullanıyor
        raise TypeError("'NoneType' object is not iterable")

    async def stream(self, request):
        self.calls += 1
        yield TextDelta(provider_id=self.provider_id, text="yarım ")
        raise TypeError("'NoneType' object is not iterable")


def test_unmapped_stream_exception_is_ledgered_and_failed_over():
    """Akış yolunda da `GatewayError` DIŞINDAKİ hata kaçmamalı.

    `complete()` ile aynı delik: defter satırı yok, kesici sayacı yok, failover yok —
    üstelik burada tüketici çoktan yarım metin görmüş oluyor, yani restart sözleşmesi
    de sessizce ihlal ediliyordu.
    """
    ledger = InMemoryAttemptLedger()
    broken = BrokenStreamAdapter()
    secondary = ScriptedProvider("secondary", [Answer(SECONDARY_TEXT)])
    router = build(broken, secondary, ledger=ledger)

    events = asyncio.run(collect(router))

    assert broken.calls == 1, "şüpheli adaptörün kendisi; aynı adaptöre tekrar YOK"
    assert events[-1].completion.provider_id == "secondary"
    assert ledger.records[0].error_class == "TypeError"
    assert ledger.records[0].discarded_chars == len("yarım ")
    restart = next(event for event in events if isinstance(event, StreamRestarted))
    assert restart.from_provider == "broken", "yarım metin çıktıysa restart BORÇTUR"
    assert restart.discarded_chars == len("yarım ")


def test_stream_end_completion_carries_the_request_id():
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [Answer(SECONDARY_TEXT)])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(primary, secondary, ledger=ledger)

    events = asyncio.run(collect(router))

    assert events[-1].completion.request_id == "req-s1"
    assert {record.request_id for record in ledger.records} == {"req-s1"}


# --------------------------------------------------------------- usage / errors


def test_stream_usage_is_exact_when_the_provider_reported_it():
    reported = Usage(input_tokens=9, output_tokens=21, exact=True)
    primary = ScriptedProvider("primary", [Answer(SECONDARY_TEXT, usage=reported)])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(primary, secondary)

    events = asyncio.run(collect(router))

    assert events[-1].completion.usage == Usage(input_tokens=9, output_tokens=21, exact=True)


def test_stream_usage_is_inexact_when_nobody_counted():
    primary = ScriptedProvider("primary", [Answer(SECONDARY_TEXT)])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(primary, secondary)

    events = asyncio.run(collect(router))

    assert events[-1].completion.usage.exact is False


def test_non_idempotent_ambiguous_stream_failure_is_raised():
    ledger = InMemoryAttemptLedger()
    primary = ScriptedProvider("primary", [PartialThenFail(PRIMARY_CHUNKS, AmbiguousTimeoutError("timeout"))])
    secondary = ScriptedProvider("secondary", [Answer("ASLA")])
    router = build(primary, secondary, ledger=ledger)

    with pytest.raises(AmbiguousTimeoutError):
        asyncio.run(collect(router, request_kwargs={"idempotent": False}))

    assert secondary.calls == 0
    assert len(ledger.records) == 1


def test_both_providers_failing_mid_stream_raises():
    primary = ScriptedProvider("primary", [PartialThenFail(PRIMARY_CHUNKS, ServerError("500"))])
    secondary = ScriptedProvider("secondary", [Fail(ServerError("500"))])
    router = build(primary, secondary)

    with pytest.raises(AllProvidersUnavailable) as info:
        asyncio.run(collect(router))

    assert info.value.queue_hint is True


def test_savings_mode_stream_still_tells_the_consumer_to_discard():
    """Şablon da 'başka bir metin'dir: kısmi çıktı görünmüşse restart BORÇTUR."""
    primary = ScriptedProvider("primary", [PartialThenFail(PRIMARY_CHUNKS, ServerError("500"))])
    secondary = ScriptedProvider("secondary", [Fail(ServerError("500"))])
    router = build(
        primary,
        secondary,
        savings_mode=StaticTemplateSavingsMode(text="Şu anda yanıt üretemiyoruz."),
    )

    events = asyncio.run(collect(router))

    restart = next(event for event in events if isinstance(event, StreamRestarted))
    assert restart.to_provider == "savings-mode"
    resetting = ""
    for event in events:
        if isinstance(event, StreamRestarted):
            resetting = ""
        elif isinstance(event, TextDelta):
            resetting += event.text
    assert resetting == events[-1].completion.text == "Şu anda yanıt üretemiyoruz."
