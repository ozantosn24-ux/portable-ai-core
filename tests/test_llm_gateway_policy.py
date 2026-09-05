"""Retry, circuit breaker and taxonomy units — no router, no provider, no real time.

Every timing assertion here checks the duration the policy *asked for*, not a measured
elapsed time. A resilience test that sleeps for real is a test nobody runs twice.
"""

from __future__ import annotations

import asyncio

import pytest

from wozto_ai_reference.llm_gateway import (
    AmbiguousTimeoutError,
    AuthError,
    BadRequestError,
    CircuitBreaker,
    ContentPolicyError,
    ProviderTimeoutError,
    RateLimitError,
    RetryPolicy,
    ServerError,
    Usage,
    reissue_allowed,
)


class FakeClock:
    """Monotonic time the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleeper:
    """Records requested sleep durations and returns immediately."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.calls: list[float] = []
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)


# --------------------------------------------------------------------- RetryPolicy


def test_retry_after_is_honored_over_the_local_backoff():
    """ASIL KAPI: sunucu 'ne zaman gel' dediyse formül DEĞİL o geçerlidir."""
    sleeper = RecordingSleeper()
    policy = RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=30.0, sleep=sleeper)
    error = RateLimitError("429", retry_after_s=2.5)

    slept = asyncio.run(policy.wait(1, error))

    assert slept == 2.5
    assert sleeper.calls == [2.5], "yerel üstel geri çekilme sunucunun süresini ezmemeli"


def test_retry_after_is_clamped_to_max_delay():
    """Sınırsız bir sunucu değeri isteği saatlerce park edemez."""
    policy = RetryPolicy(base_delay_s=0.5, max_delay_s=8.0)
    assert policy.delay_for(1, RateLimitError("429", retry_after_s=86400.0)) == 8.0


def test_retry_after_can_be_switched_off():
    policy = RetryPolicy(base_delay_s=0.5, max_delay_s=30.0, honor_retry_after=False)
    assert policy.delay_for(1, RateLimitError("429", retry_after_s=2.5)) == 0.5


def test_backoff_is_exponential_and_capped():
    policy = RetryPolicy(base_delay_s=0.5, max_delay_s=2.0)
    assert [policy.delay_for(attempt) for attempt in (1, 2, 3, 4)] == [0.5, 1.0, 2.0, 2.0]


def test_jitter_only_reduces_the_delay():
    """Jitter yukarı saçarsa `max_delay_s` sözleşmesi sessizce bozulur."""
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=1.0, jitter=0.5, random_source=lambda: 1.0)
    assert policy.delay_for(1) == pytest.approx(0.5)
    full = RetryPolicy(base_delay_s=1.0, max_delay_s=1.0, jitter=0.5, random_source=lambda: 0.0)
    assert full.delay_for(1) == pytest.approx(1.0)


def test_invalid_policy_configuration_is_rejected_loudly():
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="jitter"):
        RetryPolicy(jitter=1.5)
    with pytest.raises(ValueError, match="max_delay_s"):
        RetryPolicy(base_delay_s=5.0, max_delay_s=1.0)


# ------------------------------------------------------------------ CircuitBreaker


def test_breaker_opens_after_the_threshold_and_refuses_requests():
    breaker = CircuitBreaker(failure_threshold=3, open_seconds=10.0, clock=FakeClock())
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state == "closed"
    assert breaker.allows_request() is True

    breaker.record_failure()

    assert breaker.state == "open"
    assert breaker.allows_request() is False


def test_breaker_half_opens_after_the_window_and_closes_on_success():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=10.0, clock=clock)
    breaker.record_failure()

    clock.advance(9.9)
    assert breaker.allows_request() is False

    clock.advance(0.2)
    assert breaker.allows_request() is True
    assert breaker.state == "half_open"

    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.failures == 0


def test_half_open_admits_only_the_configured_number_of_probes():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=1.0, half_open_max_calls=2, clock=clock)
    breaker.record_failure()
    clock.advance(2.0)

    assert [breaker.allows_request() for _ in range(3)] == [True, True, False]


def test_one_failed_probe_reopens_immediately():
    """Yarı-açıkta tek hata, eşiği yeniden doldurmayı BEKLEMEZ."""
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=5, open_seconds=10.0, clock=clock)
    for _ in range(5):
        breaker.record_failure()
    clock.advance(11.0)
    assert breaker.allows_request() is True

    breaker.record_failure()

    assert breaker.state == "open"
    assert breaker.allows_request() is False


# --------------------------------------------------------------------------- Usage


def test_one_estimate_makes_the_whole_sum_an_estimate():
    exact = Usage(input_tokens=10, output_tokens=5, exact=True)
    estimate = Usage(input_tokens=3, output_tokens=1, exact=False)

    total = exact + estimate

    assert (total.input_tokens, total.output_tokens) == (13, 6)
    assert total.exact is False, "tahmin içeren toplam 'kesin' etiketiyle faturaya giremez"
    assert (exact + exact).exact is True


def test_usage_defaults_to_inexact():
    """Kimse saymadıysa varsayılan 'kesin' OLAMAZ."""
    assert Usage().exact is False


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("error", "idempotent", "expected"),
    [
        # Yan etkisi olan istek: yalnız 'gönderilmedi' KANITI yeniden göndertir.
        (RateLimitError("429"), False, True),
        (ServerError("503", pre_send=True), False, True),
        (ServerError("500"), False, False),
        (ProviderTimeoutError("timeout"), False, False),
        (AmbiguousTimeoutError("timeout after send"), False, False),
        # Saf üretim: belirsizlik zararsız, tekrar serbest.
        (AmbiguousTimeoutError("timeout after send"), True, True),
        (ProviderTimeoutError("timeout"), True, True),
        (ServerError("500"), True, True),
        # İstek kusuru: hiçbir koşulda tekrar edilmez.
        (BadRequestError("400"), True, False),
        (ContentPolicyError("refused"), True, False),
        # Kimlik: yeniden GÖNDERİM serbest (istek işlenmedi); aynı sağlayıcıya
        # tekrar denemeyi yasaklayan kural yönlendiricide.
        (AuthError("401"), False, True),
    ],
)
def test_reissue_allowed_matrix(error, idempotent, expected):
    assert reissue_allowed(idempotent=idempotent, error=error) is expected


def test_negative_retry_after_is_rejected():
    with pytest.raises(ValueError, match="retry_after_s"):
        RateLimitError("429", retry_after_s=-1.0)
