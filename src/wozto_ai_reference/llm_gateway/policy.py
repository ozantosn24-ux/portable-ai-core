"""Retry backoff and per-provider circuit breaking, both with injectable time.

Time and sleeping are constructor arguments, not module-level calls. A resilience
component whose tests have to sleep in real time gets tested at one timing and then
never again, because nobody wants a 40-second suite. Injecting them makes the
*decision* observable: a test asserts the exact duration that was requested instead of
guessing from a stopwatch.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from .errors import RetryableError
from .types import ChatRequest, Completion, Usage

SAVINGS_MODE_PROVIDER_ID = "savings-mode"

# İKİ AYRI SAAT, iki ayrı iş — birleştirilirse ikisi de yanlış olur.
# `Clock` MONOTONİKTİR: yalnız SÜRE ölçer, başlangıç noktası keyfidir (bu makinede
# ~229779.27 saniye) ve takvimle ilgisi yoktur. `WallClock` takvim zamanıdır (epoch
# saniye, `time.time`): "bu ne zaman oldu" sorusunun tek cevabı odur, ama geriye
# atlayabilir (NTP düzeltmesi, saat değişimi) ⇒ süre ölçümünde KULLANILAMAZ.
Clock = Callable[[], float]
WallClock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
BreakerState = Literal["closed", "open", "half_open"]


class RetryPolicy:
    """Exponential backoff with optional jitter and `Retry-After` deference.

    `honor_retry_after` exists because a provider that tells you when to come back is
    giving you better information than any local formula. It is still clamped to
    `max_delay_s`: an unbounded server value (a misconfigured proxy answering
    `Retry-After: 86400`) would otherwise park a caller's request for a day inside what
    looks like a fast code path.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay_s: float = 0.5,
        max_delay_s: float = 8.0,
        jitter: float = 0.0,
        honor_retry_after: bool = True,
        sleep: Sleeper | None = None,
        random_source: Callable[[], float] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if base_delay_s < 0 or max_delay_s < 0:
            raise ValueError("delays must not be negative")
        if max_delay_s < base_delay_s:
            raise ValueError("max_delay_s must not be smaller than base_delay_s")
        if not 0.0 <= jitter <= 1.0:
            raise ValueError("jitter must be a fraction between 0 and 1")
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.jitter = jitter
        self.honor_retry_after = honor_retry_after
        self._sleep: Sleeper = sleep if sleep is not None else asyncio.sleep
        self._random = random_source if random_source is not None else random.random

    def delay_for(self, attempt: int, error: BaseException | None = None) -> float:
        """Seconds to wait before attempt number `attempt + 1`."""

        if attempt < 1:
            raise ValueError("attempt numbering starts at 1")
        if self.honor_retry_after and isinstance(error, RetryableError) and error.retry_after_s is not None:
            return min(error.retry_after_s, self.max_delay_s)
        delay = min(self.base_delay_s * (2 ** (attempt - 1)), self.max_delay_s)
        if self.jitter:
            # Jitter yalnız AŞAĞI çeker: yukarı da saçmak tavanı aşar ve `max_delay_s`
            # sözleşmesini sessizce bozar. Amaç zaten eşzamanlı istemcilerin aynı
            # milisaniyede geri gelmesini (thundering herd) kırmaktır.
            delay -= delay * self.jitter * self._random()
        return delay

    async def wait(self, attempt: int, error: BaseException | None = None) -> float:
        """Sleep before the next attempt and return the duration that was requested."""

        delay = self.delay_for(attempt, error)
        await self._sleep(delay)
        return delay


class CircuitBreaker:
    """Per-provider failure fence.

    A provider that has failed N times in a row is almost certainly still failing. Left
    alone, every request pays its full timeout before failover, so an outage on the
    primary turns into latency on *every* request rather than a fast switch. While the
    breaker is open the provider is not called at all.

    States: `closed` (normal) -> `open` after `failure_threshold` consecutive failures
    -> `half_open` once `open_seconds` have passed, admitting at most
    `half_open_max_calls` probes. One probe failure re-opens immediately; a probe
    success closes it and clears the failure count.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        open_seconds: float = 30.0,
        half_open_max_calls: int = 1,
        clock: Clock | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if open_seconds < 0:
            raise ValueError("open_seconds must not be negative")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be at least 1")
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.half_open_max_calls = half_open_max_calls
        self._clock: Clock = clock if clock is not None else time.monotonic
        self._state: BreakerState = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    def allows_request(self) -> bool:
        """Ask permission for ONE attempt, advancing the state machine as a side effect.

        ⚠️ Yan etkilidir: `open -> half_open` geçişini burada yapar ve half-open'da
        verilen izni SAYAR. Sırf "durum ne" diye çağırma — onun için `state` var, ve
        her çağrı yarı-açık kotasından bir hak yer. Ayrı bir `tick()`/`record_attempt()`
        çifti koymadık: iki adımlı protokolde ikinciyi çağırmayı unutmak kotayı
        sessizce sonsuz yapardı.
        """

        if self._state == "closed":
            return True
        if self._state == "open":
            if self._clock() - self._opened_at < self.open_seconds:
                return False
            self._state = "half_open"
            self._half_open_calls = 0
        if self._half_open_calls >= self.half_open_max_calls:
            return False
        self._half_open_calls += 1
        return True

    def record_success(self) -> None:
        self._state = "closed"
        self._failures = 0
        self._half_open_calls = 0

    def record_failure(self) -> None:
        if self._state == "half_open":
            self._trip()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = "open"
        self._opened_at = self._clock()
        self._half_open_calls = 0


class SavingsMode(Protocol):
    """Last-resort template source used when every provider is unavailable.

    Returning `None` means "I have nothing useful for this request", and the gateway
    then raises `AllProvidersUnavailable` as if no savings mode were configured. A
    template that pretends to be an answer is worse than an error.
    """

    def completion_for(self, request: ChatRequest) -> Completion | None: ...


class StaticTemplateSavingsMode:
    """Serve one fixed, honest string instead of an answer.

    ⛔ This is a degraded-service notice, not a fallback answer. It must never be
    phrased as if the model produced it — `provider_id` is `savings-mode` precisely so
    every downstream consumer, log line and ledger row can tell the difference.
    """

    def __init__(self, *, text: str, model: str = "savings-mode-template") -> None:
        if not text.strip():
            raise ValueError("savings mode template text must not be empty")
        self._text = text
        self._model = model

    def completion_for(self, request: ChatRequest) -> Completion | None:
        return Completion(
            text=self._text,
            provider_id=SAVINGS_MODE_PROVIDER_ID,
            model=self._model,
            # Sıfır token HARCANDI ve bu KESİN olarak bilinen bir sayıdır — tahmin
            # değil. `exact=False` yazmak, "ölçemedik" ile "ölçtük, sıfırdı"yı
            # karıştırırdı.
            usage=Usage(input_tokens=0, output_tokens=0, exact=True),
            attempts=1,
        )
