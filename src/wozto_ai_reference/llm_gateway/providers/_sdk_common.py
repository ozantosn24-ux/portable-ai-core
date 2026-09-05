"""Shared helpers for the two SDK-backed adapters.

Both official SDKs are generated from the same toolchain, so their exception trees are
shaped identically (`APIStatusError` carrying `status_code` and `response`, with
`APITimeoutError` a subclass of `APIConnectionError`). The *status -> taxonomy* mapping
is therefore shared; only the vendor-specific parts live in each adapter.
"""

from __future__ import annotations

from typing import Any

from ..errors import (
    AmbiguousServerError,
    AmbiguousTimeoutError,
    AuthError,
    BadRequestError,
    GatewayError,
    ProviderConnectionError,
    ProviderDependencyMissing,
    RateLimitError,
    ServerError,
)

# ⚠️ 5xx İKİYE AYRILIR ve ayrım keyfi değil, "işlendi mi" sorusunun cevabıdır:
#   503 / 529 -> sunucu işi ALMADIĞINI söylüyor  => pre-send, yeniden gönderim güvenli
#   500 / 502 / 504 -> istek bir yere ULAŞTI ve orada kırıldı => BELİRSİZ
# Hepsini "retryable" saymak, yan etkisi olan bir isteği 502'den sonra ikinci kez
# göndermek demektir; hepsini "belirsiz" saymak ise gerçek bir aşırı-yük durumunda
# failover'ı gereksizce bloklar.
_REFUSED_BEFORE_WORK = frozenset({503, 529})


def import_sdk(module_name: str, extra: str = "llm") -> Any:
    """Import an optional provider SDK, or fail with an actionable message.

    Raised at construction time rather than on the first request: a deployment that is
    missing a dependency should break while it is being wired, not hours later under
    load when the primary provider goes down and the failover target turns out to be
    unimportable.
    """

    try:
        return __import__(module_name)
    except ImportError as exc:
        raise ProviderDependencyMissing(
            f"{module_name!r} paketi kurulu degil. Kurulum: pip install -e \".[{extra}]\" "
            f"— cekirdek bagimliliklara DAHIL DEGILDIR, cunku bu paketin hedefi saglayici-notr "
            f"bir cekirdektir."
        ) from exc


def retry_after_seconds(error: Any) -> float | None:
    """Read the retry delay off an SDK status error, if it carries one. Returns seconds.

    Header order mirrors the SDKs' own parser (`_base_client._parse_retry_after_header`,
    read off `anthropic==1.4.0`): the non-standard **`retry-after-ms` wins**, because it
    is the precise one. `Retry-After` is specified as integer seconds, so a provider that
    means 250 ms must round it to 0 or 1 — honouring only the coarse header leaves the
    gateway either hammering a provider that asked for a pause, or idling several times
    longer than it was asked to.

    Only the numeric forms are honoured. The HTTP-date form of `retry-after` is legal but
    rare on these APIs, and parsing it wrong would produce a delay that is silently
    absurd; returning `None` falls back to the local backoff, which is merely suboptimal.
    """

    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    # Ayrıştırılamayan bir `retry-after-ms`, saniye başlığını DÜŞÜRMEZ — SDK'nın kendi
    # ayrıştırıcısı da bu durumda `retry-after`'a geçer.
    milliseconds = _non_negative_float(headers.get("retry-after-ms"))
    if milliseconds is not None:
        return milliseconds / 1000.0
    return _non_negative_float(headers.get("retry-after"))


def _non_negative_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def classify_status(
    status: int | None,
    *,
    provider_id: str,
    message: str,
    retry_after_s: float | None = None,
) -> GatewayError:
    """Map an HTTP status onto the gateway taxonomy."""

    if status is None:
        return AmbiguousServerError(message, provider_id=provider_id)
    if status == 429:
        return RateLimitError(message, provider_id=provider_id, retry_after_s=retry_after_s)
    if status in (401, 403):
        return AuthError(message, provider_id=provider_id)
    if status in _REFUSED_BEFORE_WORK:
        return ServerError(message, provider_id=provider_id, retry_after_s=retry_after_s, pre_send=True)
    if status >= 500:
        return AmbiguousServerError(message, provider_id=provider_id)
    return BadRequestError(message, provider_id=provider_id)


def classify_transport(sdk: Any, error: Exception, *, provider_id: str) -> GatewayError:
    """Map a transport failure.

    A **timeout** is ambiguous by construction: we are waiting because the request was
    already sent, and the deadline expiring tells us nothing about what the server did
    with it. A non-timeout connection failure is retryable but still not claimed as
    pre-send — the SDK wraps "could not connect" and "connection dropped mid-response"
    in the same class, and only the first of those is provably harmless.
    """

    if isinstance(error, sdk.APITimeoutError):
        return AmbiguousTimeoutError(str(error) or "provider timeout", provider_id=provider_id)
    return ProviderConnectionError(str(error) or "provider connection failed", provider_id=provider_id)
