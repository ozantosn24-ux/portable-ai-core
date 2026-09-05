"""Error taxonomy: the router routes on *class*, never on a message string.

Four questions decide what the gateway is allowed to do next, and each one is a
separate branch of this tree:

1. Is another attempt worth anything?      -> `RetryableError` vs `NonRetryableError`
2. Is the credential the problem?          -> `AuthError` (retrying the same provider
                                              can only fail the same way)
3. Can we prove the request never landed?  -> `RetryableError.pre_send`
4. Might it have landed already?           -> `AmbiguousOutcomeError`

Adapters translate their SDK's exceptions into exactly one of these. An adapter that
cannot tell (3) from (4) must choose (4): claiming "not sent" without proof is how a
gateway silently bills a customer twice.
"""

from __future__ import annotations


class GatewayError(Exception):
    """Base class for every failure the gateway itself understands."""

    def __init__(self, message: str, *, provider_id: str | None = None) -> None:
        super().__init__(message)
        self.provider_id = provider_id


class RetryableError(GatewayError):
    """A failure where another attempt has a real chance of succeeding.

    `pre_send` means the protocol *proved* the request was refused before any
    processing (a 429 quota rejection, a 503 "not accepting work"). Only then may a
    non-idempotent request be reissued. It defaults to False on purpose: absence of
    evidence that the request landed is not evidence that it did not.

    `retry_after_s` carries a server-supplied wait when the protocol gave one.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_id: str | None = None,
        retry_after_s: float | None = None,
        pre_send: bool = False,
    ) -> None:
        super().__init__(message, provider_id=provider_id)
        if retry_after_s is not None and retry_after_s < 0:
            raise ValueError("retry_after_s must not be negative")
        self.retry_after_s = retry_after_s
        self.pre_send = pre_send


class RateLimitError(RetryableError):
    """HTTP 429. Pre-send by definition: the provider declined to start the work."""

    def __init__(
        self,
        message: str = "provider rate limit",
        *,
        provider_id: str | None = None,
        retry_after_s: float | None = None,
        pre_send: bool = True,
    ) -> None:
        super().__init__(message, provider_id=provider_id, retry_after_s=retry_after_s, pre_send=pre_send)


class ServerError(RetryableError):
    """A 5xx the adapter can argue was never processed (503 / 529 style refusals).

    A 500 or 502 does NOT belong here — the request reached something that ran it far
    enough to break. Those map to `AmbiguousServerError`.
    """


class ProviderTimeoutError(RetryableError):
    """The call ran out of time. Ambiguous unless the adapter can prove pre-send."""


class ProviderConnectionError(RetryableError):
    """Transport-level failure (DNS, TLS, refused or dropped connection)."""


class NonRetryableError(GatewayError):
    """The request itself is the defect. Neither a retry nor a different provider
    fixes it, so the gateway raises immediately instead of burning quota twice."""


class BadRequestError(NonRetryableError):
    """HTTP 400 / 422 — malformed or rejected parameters."""


class ContentPolicyError(NonRetryableError):
    """The provider declined on content grounds.

    ⛔ Failover is NOT a remedy here. Shopping a refused prompt around providers
    until one answers is policy laundering, and it also hides the refusal from the
    caller who needs to see it. The caller decides what to do, not the router.
    """

    def __init__(
        self,
        message: str = "provider declined on content policy",
        *,
        provider_id: str | None = None,
        category: str | None = None,
    ) -> None:
        super().__init__(message, provider_id=provider_id)
        self.category = category


class AuthError(GatewayError):
    """HTTP 401 / 403 — this provider's credential is unusable right now.

    ⚠️ Never retried against the same provider: the credential will not become valid
    between two attempts milliseconds apart, so a retry only adds latency and log
    noise. Failover is immediate, because a second provider has a different credential
    and may well work.
    """


class AmbiguousOutcomeError(GatewayError):
    """The request may or may not have been processed, and we cannot find out.

    This is the error class that exists to protect the caller from the gateway. For an
    `idempotent=True` request nothing is at stake — worst case the model generates the
    same paragraph twice and one copy is dropped. For `idempotent=False` the caller has
    already bound a side effect to the result, so a second execution means a second
    email, a second charge, a second row. The gateway cannot undo that and must not
    risk it: it stops and hands the ambiguity to the caller, who is the only party that
    knows how to reconcile it (check the downstream system, or accept the duplicate).

    ⛔ Do not "helpfully" retry this class. Silence here is not safety.
    """


class AmbiguousTimeoutError(AmbiguousOutcomeError):
    """Timed out after the request was (or may have been) transmitted."""


class AmbiguousServerError(AmbiguousOutcomeError):
    """A 5xx from a server that had already accepted the request body."""


class UnclassifiedProviderError(AmbiguousOutcomeError):
    """An adapter raised something that is not in this taxonomy at all.

    Adapters do real work *outside* their own `try`: reading response blocks, joining
    choices, building a `Completion`. A `TypeError` from that code is not a gateway
    error, and before this class existed it escaped the router unmapped — no ledger row,
    no circuit-breaker failure, no failover. The gateway then looked healthy while the
    caller received a raw vendor-shaped traceback.

    It sits under `AmbiguousOutcomeError` deliberately. An unmapped exception says
    nothing about whether the request landed: the adapter may well have raised it while
    parsing a response the provider had already produced and billed. Putting it on the
    ambiguous branch means an `idempotent=False` caller is protected by the rule that
    already exists, instead of depending on someone remembering a special case.

    ⛔ Never retried against the SAME provider — the suspect is the adapter, so a second
    call reproduces the same defect while paying for the request twice. Failing over to
    a different adapter is the only move with a chance of working, and only when the
    caller can absorb a duplicate.
    """


class AllProvidersUnavailable(GatewayError):
    """Every configured provider failed or was fenced off by its circuit breaker.

    `queue_hint` tells the caller this is a capacity/availability failure rather than a
    request defect: the sane response is to enqueue and retry later, not to rewrite the
    prompt. It is True by default because that is what exhausting a provider chain
    means.
    """

    def __init__(
        self,
        message: str = "no configured provider could serve the request",
        *,
        queue_hint: bool = True,
        provider_id: str | None = None,
    ) -> None:
        super().__init__(message, provider_id=provider_id)
        self.queue_hint = queue_hint


class ProviderDependencyMissing(GatewayError):
    """An optional provider SDK is not installed. Raised at construction time, so a
    misconfigured deployment fails at wiring rather than on the first live request."""


def reissue_allowed(*, idempotent: bool, error: BaseException) -> bool:
    """Whether this failure permits sending the request anywhere again.

    One predicate covers both same-provider retry and failover, because they carry the
    identical risk: the request going out a second time. Splitting them into two rules
    is how a codebase ends up refusing to retry a non-idempotent request and then
    quietly failing it over instead.
    """

    if isinstance(error, (NonRetryableError, AuthError)):
        # AuthError'da yeniden GONDERIM guvenlidir (istek islenmedi); yasak olan AYNI
        # saglayiciya tekrar denemektir ve o karar yonlendiricidedir, burada degil.
        return not isinstance(error, NonRetryableError)
    if isinstance(error, AmbiguousOutcomeError):
        return idempotent
    if isinstance(error, RetryableError):
        return idempotent or error.pre_send
    return False
