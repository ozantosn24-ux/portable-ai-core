# 0002 — A non-idempotent request is never reissued after an ambiguous failure

## Status

Accepted — 2026-09-06.

## Context

`ChatRequest` carries two flags that are deliberately not merged
(`src/wozto_ai_reference/llm_gateway/types.py`, `ChatRequest` docstring):

* `idempotency_key` identifies *this* logical request so a provider that supports
  idempotency keys can collapse a duplicate delivery into one execution;
* `idempotent` says whether **the caller** can absorb the result arriving twice. "Pure
  generation is idempotent. A request whose result the caller has already wired to a side
  effect (an email queued, a row written, money moved) is not, even when the provider itself
  would happily replay it."

The error taxonomy exists to answer four separate questions, and the third and fourth are
the ones this ADR turns on (`llm_gateway/errors.py`, module docstring): *can we prove the
request never landed?* (`RetryableError.pre_send`) and *might it have landed already?*
(`AmbiguousOutcomeError`). The same docstring states the adapter obligation: "An adapter
that cannot tell (3) from (4) must choose (4): claiming 'not sent' without proof is how a
gateway silently bills a customer twice."

## Decision

1. **One predicate governs both retry and failover.** `errors.reissue_allowed(*, idempotent,
   error)` answers "whether this failure permits sending the request anywhere again",
   "because they carry the identical risk: the request going out a second time. Splitting
   them into two rules is how a codebase ends up refusing to retry a non-idempotent request
   and then quietly failing it over instead."
2. **`AmbiguousOutcomeError` + `idempotent=False` ⇒ raise.** `reissue_allowed` returns
   `idempotent` for that branch; the router raises instead of retrying or failing over, and
   hands the ambiguity to the caller, "who is the only party that knows how to reconcile it"
   (`errors.AmbiguousOutcomeError` docstring).
3. **The guard is applied on both code paths.** `router.complete` and `router.stream` each
   call `reissue_allowed` before deciding anything, and `_retry_here` calls it again as its
   first gate — "'don't retry' and 'don't fail over' are the same protection".
4. **5xx is split by whether the work was accepted.**
   `providers/_sdk_common.py` maps `503` and `529` (`_REFUSED_BEFORE_WORK`) to
   `ServerError(pre_send=True)` — the server said it did not take the work — and every other
   `>= 500` to `AmbiguousServerError`. The comment states both failure modes of not
   splitting: treating all 5xx as retryable means re-sending a side-effecting request after a
   502; treating all of them as ambiguous blocks failover during a genuine overload.
5. **Timeouts are ambiguous by construction.** `classify_transport` maps
   `APITimeoutError` to `AmbiguousTimeoutError`: "we are waiting because the request was
   already sent, and the deadline expiring tells us nothing about what the server did with
   it." A non-timeout connection failure is retryable but still not claimed as pre-send,
   because the SDK wraps "could not connect" and "connection dropped mid-response" in one
   class and only the first is provably harmless.
6. **`pre_send` defaults to `False`.** "Absence of evidence that the request landed is not
   evidence that it did not" (`errors.RetryableError` docstring). `RateLimitError` (429)
   overrides it to `True` — "pre-send by definition: the provider declined to start the work".
7. **Anything the adapter raises outside the taxonomy becomes
   `UnclassifiedProviderError`, on the ambiguous branch.** `router._as_gateway_error` wraps
   it; the class docstring gives the reason for the placement: "an unmapped exception says
   nothing about whether the request landed: the adapter may well have raised it while parsing
   a response the provider had already produced and billed. Putting it on the ambiguous branch
   means an `idempotent=False` caller is protected by the rule that already exists, instead of
   depending on someone remembering a special case." It is additionally never retried against
   the same provider, because the suspect is the adapter (`router._retry_here`).
8. **`AuthError` is reissuable but not retryable.** `reissue_allowed` returns `True` for it,
   and its inline comment says why the two halves are split: re-*sending* is safe because the
   request was not processed, while the ban on trying the same provider again "o karar
   yönlendiricidedir, burada değil" — that decision belongs to the router, not to this
   function. The reason for the ban is stated in `errors.AuthError`'s docstring: "the
   credential will not become valid between two attempts milliseconds apart, so a retry only
   adds latency and log noise", and `router._retry_here` restates it in Turkish where the ban
   is actually enforced.
9. **`NonRetryableError` (400/422, content policy) is raised, never shopped around.**
   `ContentPolicyError` adds the reason: "Shopping a refused prompt around providers until
   one answers is policy laundering, and it also hides the refusal from the caller who needs
   to see it."

## Consequences

**Positive**

* A caller who has already bound a side effect to the result cannot have that side effect
  fired twice by the gateway's own recovery logic.
* The rule is one predicate in one place, so a new call site cannot accidentally implement
  only half of it.
* Both halves of the published behaviour table are tested, not just the safe half — the
  parametrised test carries the note that testing only the `idempotent=True` column left
  "half the table unproven — and the right-hand column is exactly the column where the money
  gets spent twice".

**Negative / not solved**

* **Availability is traded for correctness.** An `idempotent=False` request hitting a 500 or
  a timeout fails on the first attempt even when the secondary provider is healthy. That is
  the intended trade, but it is a real reduction in success rate for side-effecting calls.
* **The caller inherits the reconciliation work.** The gateway stops and says "this may or
  may not have happened"; checking the downstream system, or accepting the duplicate, is the
  caller's job. Nothing in this package helps with that.
* **The classification is only as good as the adapter.** Everything here depends on adapters
  translating SDK exceptions honestly; a wrong `pre_send=True` would defeat the whole rule,
  and no test can catch an adapter that lies about a real provider's behaviour.
* **`ProviderTimeoutError` is exported but no adapter produces it.** SDK timeouts all map to
  `AmbiguousTimeoutError`; the class is kept for an adapter that can prove pre-send, and it
  has no row in the behaviour table because no path reaches it today
  (README, *Bilinen sınırlar*).
* **Never exercised against a live 429 or a live outage** (README, same section).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Retry / fail over everything that is not obviously a request defect | This is what "silently bills a customer twice" means in `errors.py`'s module docstring: without a pre-send proof, a second send can be a second charge. |
| Separate rules for "may I retry" and "may I fail over" | Rejected in `reissue_allowed`'s docstring: the two carry the identical risk, and splitting them is precisely how a codebase refuses the retry and then quietly fails the request over instead. |
| Treat every 5xx as retryable | Re-sends a side-effecting request after a 502, which reached something that ran it far enough to break (`errors.ServerError` docstring; `_sdk_common._REFUSED_BEFORE_WORK` comment). |
| Treat every 5xx as ambiguous | Blocks failover unnecessarily during a genuine overload (same comment). |
| Let an unmapped adapter exception escape the router | That was the prior behaviour and it produced "no ledger row, no circuit-breaker failure, no failover. The gateway then looked healthy while the caller received a raw vendor-shaped traceback" (`errors.UnclassifiedProviderError` docstring). |
| Give `UnclassifiedProviderError` a special case for non-idempotent callers | Rejected in the same docstring: placing it on the ambiguous branch means the existing rule protects it, "instead of depending on someone remembering a special case". |
| Fail a content refusal over to another provider | Policy laundering, and it hides the refusal from the caller (`errors.ContentPolicyError`). |

## Evidence

Source:

* `src/wozto_ai_reference/llm_gateway/errors.py` — module docstring (the four questions and
  the adapter obligation), `RetryableError.pre_send`, `RateLimitError`, `ServerError`,
  `AuthError`, `NonRetryableError`, `BadRequestError`, `ContentPolicyError`,
  `AmbiguousOutcomeError`, `AmbiguousTimeoutError`, `AmbiguousServerError`,
  `UnclassifiedProviderError`, `reissue_allowed`.
* `src/wozto_ai_reference/llm_gateway/router.py` — the two `reissue_allowed` guards (one in
  `complete`, one in `stream`, both marked `BELİRSİZ SONUÇ KAPISI`), `_retry_here`,
  `_as_gateway_error`, and the `except Exception` (not `except GatewayError`) decision with
  its comment.
* `src/wozto_ai_reference/llm_gateway/providers/_sdk_common.py` —
  `_REFUSED_BEFORE_WORK = frozenset({503, 529})`, `classify_status`, `classify_transport`.
* `src/wozto_ai_reference/llm_gateway/types.py` — `ChatRequest` docstring
  (`idempotency_key` ≠ `idempotent`).
* `README.md`, *Davranış tablosu* (both columns) and the ⭐ paragraph *Belirsiz sonuç kuralı*.
* `docs/architecture.md`, *LLM gateway*, clause (b).
* Commit `88e600e` message: "idempotent=False + ambiguous outcome (timeout after send,
  500/502/504) -> AmbiguousOutcomeError, one attempt, no retry, no failover (complete() and
  stream())".

Tests:

* `tests/test_llm_gateway_router.py` — `test_behaviour_table_non_idempotent_column`
  (parametrised: `ServerError(pre_send=True)` and `AuthError` reissue; `ProviderConnectionError`,
  `BadRequestError`, `ContentPolicyError` do not, asserting `secondary.calls == 0`) ·
  `test_non_idempotent_ambiguous_outcome_is_raised_with_a_single_attempt` ·
  `test_idempotent_request_may_retry_the_same_ambiguous_failure` ·
  `test_non_idempotent_pre_send_rate_limit_may_still_be_retried` ·
  `test_unmapped_adapter_exception_is_ledgered_and_failed_over` ·
  `test_unmapped_adapter_exception_is_ambiguous_for_a_non_idempotent_request` ·
  `test_auth_error_fails_over_immediately_without_retrying` ·
  `test_bad_request_is_raised_without_failover` ·
  `test_content_policy_refusal_is_not_shopped_around`.
* `tests/test_llm_gateway_stream.py` — `test_non_idempotent_ambiguous_stream_failure_is_raised`.
* `tests/test_llm_gateway_policy.py` — `test_reissue_allowed_matrix`.
* `tests/test_llm_gateway_adapters.py` — `test_500_is_ambiguous_not_merely_retryable` ·
  `test_anthropic_status_mapping` · `test_openai_status_mapping_uses_the_same_rules` ·
  `test_transport_errors_are_classified_by_what_we_can_prove`.

Numbers: the only literals asserted here are HTTP status codes, and they come from
`_sdk_common.classify_status` and its `_REFUSED_BEFORE_WORK` set, not from any measurement.
The router module docstring states that deleting either `reissue_allowed` guard makes named
tests fail; that mutation check is recorded there and in commit `88e600e`, and was not
re-run for this ADR.

## Related

* ADR 0001 — the other load-bearing rule in `router.py`; the stream path applies both.
* ADR 0003 — why request-side defects (`BadRequestError`, `ContentPolicyError`) are also kept
  out of the circuit breaker.
* ADR 0004 — every one of these decisions leaves a ledger row, including the ones that raise.
