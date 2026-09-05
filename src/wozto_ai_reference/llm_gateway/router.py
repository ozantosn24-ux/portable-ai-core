"""Retry -> circuit breaker -> failover, with every attempt written to a ledger.

The router owns three decisions and nothing else: *try again here*, *stop calling this
provider*, *move to the next provider*. It never edits a prompt, never merges two
providers' output, and never decides that a refusal was probably fine.

Two rules in this file are load-bearing enough that breaking either produces a bug no
test outside this module would catch. Both are spelled out where they are enforced:

* the no-concatenation rule (`stream`), and
* the ambiguous-outcome rule (`_retry_here` and both `reissue_allowed` guards).

Both are covered by mutation checks: inverting the restart-before-delta order or
deleting either `reissue_allowed` guard makes named tests fail.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from .errors import (
    AllProvidersUnavailable,
    AmbiguousOutcomeError,
    GatewayError,
    NonRetryableError,
    RetryableError,
    UnclassifiedProviderError,
    reissue_allowed,
)
from .ledger import AttemptLedger, AttemptRecord, InMemoryAttemptLedger
from .policy import CircuitBreaker, Clock, RetryPolicy, SavingsMode, WallClock
from .ports import ChatProvider, StreamUsageReporter
from .types import (
    ChatRequest,
    Completion,
    StreamEnd,
    StreamEvent,
    StreamRestarted,
    TextDelta,
    Usage,
)


class FailoverRouter:
    """Route a chat request across a primary and a secondary provider.

    `breakers` is keyed by `provider_id`; a provider without an entry gets a default
    breaker so that forgetting to configure one cannot silently disable fencing.
    """

    def __init__(
        self,
        primary: ChatProvider,
        secondary: ChatProvider,
        retry_policy: RetryPolicy | None = None,
        breakers: Mapping[str, CircuitBreaker] | None = None,
        ledger: AttemptLedger | None = None,
        *,
        savings_mode: SavingsMode | None = None,
        clock: Clock | None = None,
        wall_clock: WallClock | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if primary.provider_id == secondary.provider_id:
            # Aynı id iki sağlayıcıda = defterde iki satırın hangisine ait olduğu
            # ayırt EDİLEMEZ ve iki devre kesici tek anahtara çakışır.
            raise ValueError("primary and secondary must have distinct provider_id values")
        self._chain: tuple[ChatProvider, ...] = (primary, secondary)
        self._retry = retry_policy if retry_policy is not None else RetryPolicy()
        supplied = dict(breakers) if breakers else {}
        self._breakers = {
            provider.provider_id: supplied.get(provider.provider_id, CircuitBreaker())
            for provider in self._chain
        }
        self._ledger: AttemptLedger = ledger if ledger is not None else InMemoryAttemptLedger()
        self._savings_mode = savings_mode
        # İKİ SAAT, iki iş. `clock` MONOTONİKTİR ve yalnız `latency_ms` üretir;
        # `wall_clock` takvim zamanıdır ve yalnız `AttemptRecord.ts` üretir. Tek saatle
        # idare etmek iki yönde de bozar: monotonik saatle yazılan `ts` 1970'e düşer
        # (başlangıcı keyfidir), duvar saatiyle ölçülen süre ise bir NTP düzeltmesinde
        # negatife döner.
        self._clock: Clock = clock if clock is not None else time.monotonic
        self._wall_clock: WallClock = wall_clock if wall_clock is not None else time.time
        self._request_id_factory = request_id_factory if request_id_factory is not None else (lambda: uuid4().hex)

    @property
    def providers(self) -> Sequence[ChatProvider]:
        return self._chain

    @property
    def ledger(self) -> AttemptLedger:
        return self._ledger

    def breaker_for(self, provider_id: str) -> CircuitBreaker:
        return self._breakers[provider_id]

    # ------------------------------------------------------------------ complete

    async def complete(self, request: ChatRequest) -> Completion:
        """Return one provider's completion, or raise.

        Order per provider: retry while the policy allows -> give up on this provider
        -> next provider. A provider is skipped entirely while its breaker is open.
        """

        request_id = request.request_id or self._request_id_factory()
        attempts = 0
        for provider in self._chain:
            breaker = self._breakers[provider.provider_id]
            attempt_in_provider = 0
            while True:
                if not breaker.allows_request():
                    self._record(
                        request,
                        request_id,
                        provider_id=provider.provider_id,
                        attempt=0,
                        outcome="skipped_open_circuit",
                        latency_ms=0.0,
                    )
                    break
                attempt_in_provider += 1
                attempts += 1
                started = self._clock()
                try:
                    completion = await provider.complete(request)
                # ⛔ `GatewayError` DEĞİL, `Exception`. Adaptörler gerçek işin bir
                # kısmını kendi `try`'larının DIŞINDA yapar (blokları okumak, choice'ları
                # birleştirmek, `Completion` kurmak); oradan gelen bir `TypeError` daha
                # önce buradan kaçıyordu: defter satırı yok, kesici sayacı artmıyor,
                # failover yok. Sınıflandırılamayan hata da bir ARIZADIR ve zincirin
                # tamamından geçmelidir.
                except Exception as exc:  # noqa: BLE001 - sınıflandırılıp yeniden atılıyor
                    failure = _as_gateway_error(exc, provider.provider_id)
                    self._record(
                        request,
                        request_id,
                        provider_id=provider.provider_id,
                        attempt=attempt_in_provider,
                        outcome="error",
                        # ORİJİNAL tipin adı: defteri okuyan kişi "UnclassifiedProviderError"
                        # değil, gerçekte ne patladığını görmeli.
                        error_class=type(exc).__name__,
                        latency_ms=self._elapsed_ms(started),
                        model=request.model or provider.model,
                    )
                    _count_provider_failure(breaker, failure)
                    # ⛔ BELİRSİZ SONUÇ KAPISI. Bu iki satır kalkarsa `idempotent=False`
                    # bir istek, ilk denemenin işlenip işlenmediği BİLİNMEZKEN yedek
                    # sağlayıcıya düşer ve yan etki ikinci kez tetiklenir. "Yeniden
                    # deneme yok" ile "failover yok" AYNI korumadır; ikisi de burada.
                    if not reissue_allowed(idempotent=request.idempotent, error=failure):
                        if failure is exc:
                            raise
                        # `from exc`: sarmalanan hatanın kendisi kanıttır — çağıran
                        # `__cause__`'a bakıp gerçekte ne olduğunu görebilmeli.
                        raise failure from exc
                    if self._retry_here(request, failure, attempt_in_provider):
                        await self._retry.wait(attempt_in_provider, failure)
                        continue
                    break
                self._record(
                    request,
                    request_id,
                    provider_id=provider.provider_id,
                    attempt=attempt_in_provider,
                    outcome="ok",
                    latency_ms=self._elapsed_ms(started),
                    usage=completion.usage,
                    model=completion.model,
                )
                breaker.record_success()
                return completion.model_copy(update={"attempts": attempts, "request_id": request_id})
        return self._exhausted(request, request_id, attempts)

    # -------------------------------------------------------------------- stream

    async def stream(self, request: ChatRequest, *, buffered: bool = False) -> AsyncIterator[StreamEvent]:
        """Stream one provider's answer, restarting from scratch on failover.

        ## The no-concatenation rule

        A failed stream leaves the consumer holding a truncated prefix — half a
        sentence, an unclosed JSON object, a paragraph that stops mid-word. Splicing
        the replacement provider's output onto that prefix produces text no model ever
        wrote: duplicated openings, contradictory halves, invalid JSON. It reads like a
        model failure and is impossible to reproduce, because the seam only exists in
        the router.

        So the router never splices. On failover it emits `StreamRestarted` carrying
        exactly how many characters must be dropped, then streams the replacement from
        the beginning, and `StreamEnd.completion.text` is always one provider's
        complete output. The event is emitted *before* the first replacement delta,
        never after, so a consumer that resets its buffer on `StreamRestarted` cannot
        observe a spliced state even for one frame.

        `buffered=True` sidesteps the problem entirely by holding deltas until the
        stream succeeds: the consumer sees no partial output, so there is nothing to
        discard and `StreamRestarted` is never emitted downstream — it is recorded in
        the ledger instead. Use it when the consumer cannot roll back what it rendered.
        """

        request_id = request.request_id or self._request_id_factory()
        attempts = 0
        # (from_provider, discarded_chars) — kısmi çıktı tüketiciye ULAŞTIYSA dolar.
        pending_restart: tuple[str, int] | None = None
        for provider in self._chain:
            breaker = self._breakers[provider.provider_id]
            attempt_in_provider = 0
            while True:
                if not breaker.allows_request():
                    self._record(
                        request,
                        request_id,
                        provider_id=provider.provider_id,
                        attempt=0,
                        outcome="skipped_open_circuit",
                        latency_ms=0.0,
                    )
                    break
                attempt_in_provider += 1
                attempts += 1
                chunks: list[str] = []
                started = self._clock()
                # ⛔ TERK EDİLEN DENEME KAPISI. Tüketici `async for`dan çıkarsa (istemci
                # koptu, `break`, iptal) bu üretece `GeneratorExit` atılır: ne `ok` ne
                # `error` yolu koşar ve sağlayıcı GERÇEKTEN çağrılmış olmasına rağmen
                # defterde TEK satır kalmazdı. `resolved`, `finally`'ye "bu denemeyi
                # zaten kaydettim" diyen tek işarettir.
                resolved = False
                try:
                    try:
                        async for delta in provider.stream(request):
                            chunks.append(delta.text)
                            if buffered:
                                continue
                            # ⛔ KAPI: yedek sağlayıcının İLK delta'sı, StreamRestarted
                            # YAYINLANMADAN tüketiciye gitmez. Bu iki satır kalkarsa
                            # birleştirme kuralı sessizce çöker — tüketici kısmi metni
                            # atması gerektiğini asla öğrenmez.
                            restart = _restart_event(pending_restart, provider.provider_id)
                            if restart is not None:
                                yield restart
                                pending_restart = None
                            yield delta
                    # `Exception` — `complete()` ile aynı gerekçe: adaptörden gelen
                    # sınıflandırılamayan bir hata da defterden, kesiciden ve failover
                    # kararından geçmelidir. `GeneratorExit` bir `BaseException`'dır ve
                    # buraya DÜŞMEZ; terk edilen deneme aşağıdaki `finally`'de kaydedilir.
                    except Exception as exc:  # noqa: BLE001 - sınıflandırılıp yeniden atılıyor
                        resolved = True
                        failure = _as_gateway_error(exc, provider.provider_id)
                        discarded = len("".join(chunks))
                        self._record(
                            request,
                            request_id,
                            provider_id=provider.provider_id,
                            attempt=attempt_in_provider,
                            outcome="error",
                            latency_ms=self._elapsed_ms(started),
                            error_class=type(exc).__name__,
                            model=request.model or provider.model,
                            discarded_chars=discarded or None,
                        )
                        _count_provider_failure(breaker, failure)
                        # ⛔ BELİRSİZ SONUÇ KAPISI — `complete()` ile birebir aynı kural.
                        if not reissue_allowed(idempotent=request.idempotent, error=failure):
                            if failure is exc:
                                raise
                            raise failure from exc
                        # Aynı sağlayıcıya yeniden deneme, tüketicinin HİÇBİR ŞEY
                        # görmediği durumda anlamlıdır — o zaman deneme temiz bir
                        # istekten farksızdır. İki hâl bunu sağlar:
                        #   * hiç delta üretilmedi, ya da
                        #   * `buffered=True` ⇒ üretilen deltalar İÇERİDE tutuldu,
                        #     tüketiciye ulaşmadı; iç tampon atılır (`chunks` bir
                        #     sonraki turda sıfırdan kurulur) ve tekrar denenir.
                        # Tüketici metni GÖRDÜKTEN sonraki hata artık "yeniden deneme"
                        # değil RESTART'tır; sözleşmesi (olay + sıfırdan akış) bir
                        # sonraki sağlayıcıda işletilir.
                        if (buffered or not chunks) and self._retry_here(request, failure, attempt_in_provider):
                            await self._retry.wait(attempt_in_provider, failure)
                            continue
                        if discarded and not buffered:
                            pending_restart = (provider.provider_id, discarded)
                        break
                    else:
                        text = "".join(chunks)
                        usage = _stream_usage(provider)
                        completion = Completion(
                            text=text,
                            provider_id=provider.provider_id,
                            model=request.model or provider.model,
                            usage=usage,
                            attempts=attempts,
                            request_id=request_id,
                        )
                        self._record(
                            request,
                            request_id,
                            provider_id=provider.provider_id,
                            attempt=attempt_in_provider,
                            outcome="ok",
                            latency_ms=self._elapsed_ms(started),
                            usage=usage,
                            model=completion.model,
                        )
                        # Satır YAZILDI ⇒ deneme çözüldü. Tüketici bundan sonraki
                        # yield'lerde çekilse bile `abandoned` satırı EKLENMEZ: çağrı
                        # başarıyla bitmişti ve defterde zaten doğru satır duruyor.
                        resolved = True
                        breaker.record_success()
                        if buffered:
                            for chunk in chunks:
                                yield TextDelta(provider_id=provider.provider_id, text=chunk)
                        # Sıfır delta üreten başarılı bir akış da restart'ı BORÇLUDUR:
                        # tüketici hâlâ birincinin kısmi metnini tutuyor olabilir.
                        restart = _restart_event(pending_restart, provider.provider_id)
                        if restart is not None:
                            yield restart
                            pending_restart = None
                        yield StreamEnd(completion=completion)
                        return
                finally:
                    if not resolved:
                        # ⚠️ Burada YIELD YOK ve `GeneratorExit` YUTULMUYOR — yalnız tek
                        # bir senkron defter yazımı. Terk edilmiş bir denemede kesiciye
                        # DOKUNULMAZ: sağlayıcı başarısız olmadı, tüketici gitti.
                        self._record(
                            request,
                            request_id,
                            provider_id=provider.provider_id,
                            attempt=attempt_in_provider,
                            outcome="abandoned",
                            latency_ms=self._elapsed_ms(started),
                            model=request.model or provider.model,
                            usage=_reported_stream_usage(provider),
                            delivered_chars=len("".join(chunks)),
                        )
        fallback = self._exhausted(request, request_id, attempts)
        restart = _restart_event(pending_restart, fallback.provider_id)
        if restart is not None:
            yield restart
        yield TextDelta(provider_id=fallback.provider_id, text=fallback.text)
        yield StreamEnd(completion=fallback)

    # ------------------------------------------------------------------ internals

    def _retry_here(self, request: ChatRequest, error: GatewayError, attempt: int) -> bool:
        """Whether to try the SAME provider again.

        Three independent gates, all of which must pass. The first is the ambiguous
        outcome rule: `reissue_allowed` refuses to let a non-idempotent request go out
        twice unless the failure proves the first attempt never landed. It is checked
        here and again on the failover path, because "don't retry" and "don't fail
        over" are the same protection — a request that must not be sent twice must not
        be sent twice *to someone else* either.
        """

        if not reissue_allowed(idempotent=request.idempotent, error=error):
            return False
        if isinstance(error, UnclassifiedProviderError):
            # Şüpheli SAĞLAYICI değil ADAPTÖRDÜR: aynı adaptörü ikinci kez çağırmak aynı
            # kusuru birebir üretir, ama isteğin bedelini bir kez daha ödetir. Buradan
            # çıkış yolu failover'dır (ve o da yalnız çağıran kopyayı sindirebiliyorsa).
            return False
        if not isinstance(error, (RetryableError, AmbiguousOutcomeError)):
            # AuthError buraya düşer: kimlik bilgisi iki deneme arasında geçerli hale
            # gelmez, yalnız gecikme eklenir. Doğru hamle DERHAL failover.
            return False
        return attempt < self._retry.max_attempts

    def _exhausted(self, request: ChatRequest, request_id: str, attempts: int) -> Completion:
        if self._savings_mode is not None:
            template = self._savings_mode.completion_for(request)
            if template is not None:
                served = template.model_copy(update={"attempts": attempts, "request_id": request_id})
                self._record(
                    request,
                    request_id,
                    provider_id=served.provider_id,
                    attempt=0,
                    outcome="savings_mode",
                    latency_ms=0.0,
                    usage=served.usage,
                    model=served.model,
                )
                return served
        raise AllProvidersUnavailable(queue_hint=True)

    def _elapsed_ms(self, started: float) -> float:
        return max((self._clock() - started) * 1000.0, 0.0)

    def _record(
        self,
        request: ChatRequest,
        request_id: str,
        *,
        provider_id: str,
        attempt: int,
        outcome: str,
        latency_ms: float,
        error_class: str | None = None,
        usage: Usage | None = None,
        model: str | None = None,
        discarded_chars: int | None = None,
        delivered_chars: int | None = None,
    ) -> None:
        self._ledger.append(
            AttemptRecord(
                # ⚠️ `_wall_clock`, `_clock` DEĞİL. Monotonik saatle yazılsaydı her satır
                # 1970'e düşerdi (bkz. `AttemptRecord.ts`).
                ts=_iso_utc(self._wall_clock()),
                request_id=request_id,
                idempotency_key=request.idempotency_key,
                idempotent=request.idempotent,
                provider_id=provider_id,
                attempt=attempt,
                outcome=outcome,  # type: ignore[arg-type]
                error_class=error_class,
                latency_ms=latency_ms,
                usage=usage,
                model=model,
                discarded_chars=discarded_chars,
                delivered_chars=delivered_chars,
            )
        )


def _iso_utc(epoch_seconds: float) -> str:
    """Epoch seconds -> ISO-8601 UTC string, which is what the ledger stores.

    A string rather than a number, and UTC rather than local time, because the ledger is
    read by a human after an incident and compared against a provider's dashboard. A
    bare float forces every reader to guess an epoch and a zone; `2026-09-06T09:14:02+00:00`
    forces nobody.
    """

    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


def _as_gateway_error(exc: Exception, provider_id: str) -> GatewayError:
    """Return the taxonomy error for `exc`, wrapping anything unmapped.

    A `GatewayError` is already the adapter's own verdict and is passed through
    untouched. Everything else is a defect the gateway cannot classify, and it becomes
    `UnclassifiedProviderError` — ambiguous by construction, because an unmapped
    exception is no evidence that the request failed to land.
    """

    if isinstance(exc, GatewayError):
        return exc
    return UnclassifiedProviderError(
        f"{provider_id} adapter raised an unmapped {type(exc).__name__}: {exc}",
        provider_id=provider_id,
    )


def _count_provider_failure(breaker: CircuitBreaker, error: GatewayError) -> None:
    """Count the failure against the provider — unless the REQUEST is the defect.

    ⛔ `BadRequestError` / `ContentPolicyError` say nothing about the provider's health.
    Counting them opens the circuit of a perfectly healthy provider, and the next
    unrelated caller is then fenced off from it: one malformed prompt (or a batch of
    refused ones) takes the primary offline for `open_seconds` while nothing is actually
    wrong with it. The breaker exists to fence a FAILING PROVIDER, not a failing request.
    """

    if isinstance(error, NonRetryableError):
        return
    breaker.record_failure()


def _restart_event(pending: tuple[str, int] | None, to_provider: str) -> StreamRestarted | None:
    if pending is None:
        return None
    return StreamRestarted(from_provider=pending[0], to_provider=to_provider, discarded_chars=pending[1])


def _reported_stream_usage(provider: ChatProvider) -> Usage | None:
    """What the adapter actually reported, or `None` when it reported nothing.

    `None` and `Usage(0, 0)` are different answers and the ledger must keep them apart:
    the first is "nobody counted", the second is "counted, and it was zero". A ledger row
    for an abandoned attempt carries the honest `None`.
    """

    if isinstance(provider, StreamUsageReporter):
        return provider.last_stream_usage()
    return None


def _stream_usage(provider: ChatProvider) -> Usage:
    """Exact counts when the adapter captured them, an honest estimate flag otherwise."""

    reported = _reported_stream_usage(provider)
    if reported is not None:
        return reported
    # Sağlayıcı bildirmedi ⇒ sayı YOK. Uydurulmuş bir tahmin `exact=True` ile
    # yazılsaydı fatura raporu onu ölçüm sanardı.
    return Usage()
