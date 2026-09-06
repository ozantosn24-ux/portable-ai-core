# 0001 — Provider failover only at a request boundary; never splice two providers' output

## Status

Accepted — 2026-09-06.

## Context

`FailoverRouter` routes one chat request across a primary and a secondary provider and
owns three decisions and nothing else: *try again here*, *stop calling this provider*,
*move to the next provider*
(`src/wozto_ai_reference/llm_gateway/router.py`, module and class docstrings).

Streaming makes failover dangerous in a way that non-streaming does not. As the
`FailoverRouter.stream` docstring records it:

> A failed stream leaves the consumer holding a truncated prefix — half a sentence, an
> unclosed JSON object, a paragraph that stops mid-word. Splicing the replacement
> provider's output onto that prefix produces text no model ever wrote: duplicated
> openings, contradictory halves, invalid JSON. It reads like a model failure and is
> impossible to reproduce, because the seam only exists in the router.

The same file's module docstring names this the *no-concatenation rule* and marks it, with
the ambiguous-outcome rule (ADR 0002), as one of "two rules in this file … load-bearing
enough that breaking either produces a bug no test outside this module would catch".

## Decision

1. **The router never splices.** `StreamEnd.completion.text` is always exactly one
   provider's full output, on every path (`llm_gateway/types.py`, `StreamEnd` docstring).
2. **On failover after partial output, `StreamRestarted` is emitted before any replacement
   delta.** The event carries `from_provider`, `to_provider` and `discarded_chars` — "exactly
   how many characters the consumer must throw away" (`types.py`, `StreamRestarted`
   docstring). The gate sits inside the delta loop in `router.stream`, guarded by a comment
   that says removing it makes the no-splice rule "collapse silently".
3. **The debt survives every exit path.** A successful replacement stream that produced zero
   deltas still emits the pending restart before `StreamEnd`, and the exhausted /
   savings-mode path emits it before its single `TextDelta`
   (`router.stream`, the two later `_restart_event` calls).
4. **Failure before the first delta is transparent.** Nothing reached the consumer, so no
   event is emitted and the replacement simply streams.
5. **`buffered=True` sidesteps the problem entirely.** Deltas are held until the stream
   succeeds, so the consumer never sees partial output; `StreamRestarted` is not emitted
   downstream and is recorded in the ledger instead
   (`router.stream` docstring; `AttemptRecord.discarded_chars` in `llm_gateway/ledger.py`).
6. **Same-provider retry is allowed only while the consumer has seen nothing** — either no
   delta was produced, or `buffered=True` held them internally. After the consumer has seen
   text, the next failure is a *restart*, not a retry, and its contract (event + stream from
   zero) is executed against the next provider (`router.stream`, the `(buffered or not chunks)`
   condition and the comment above it).

## Consequences

**Positive**

* A consumer that resets its buffer on `StreamRestarted` ends up holding one provider's
  complete text; the test runs both consumers against the *same* event sequence and asserts
  the naive one is wrong and the resetting one matches `StreamEnd.completion.text`
  (`test_a_naive_concatenating_consumer_is_wrong_and_a_resetting_one_is_right`).
* Because the event precedes the first replacement delta, a resetting consumer cannot
  observe a spliced state "even for one frame" (`router.stream` docstring).
* Buffered mode preserves the retry budget: since the consumer saw nothing, the reason for
  the "no retry after output" rule does not apply and the primary is not abandoned on the
  first micro-outage (README, *Akış (stream) sözleşmesi*, item 6).

**Negative / not solved**

* **The router can announce, not enforce.** An unbuffered consumer that ignores
  `StreamRestarted` still ends up with text no model wrote — that is exactly what the naive
  half of the two-consumer test demonstrates.
* **Buffered mode gives up incremental output.** The deltas are held until the stream
  succeeds, so a consumer that wanted progressive rendering cannot have both that and the
  guarantee that it never sees a partial.
* **Discarded output is recorded, not undone.** `discarded_chars` says how much text was
  thrown away; nothing in the repo claims anything about the cost of the abandoned attempt.
* **Truncation is still invisible.** `stop_reason == "max_tokens"` (Anthropic) /
  `finish_reason == "length"` (OpenAI) is neither written to a field nor turned into an
  error, so a half answer can reach the caller looking like a whole one — listed as an open
  item in README, *Bilinen sınırlar (henüz KAPATILMADI)*.
* **Never exercised against a live outage.** All adapter tests use fake SDK clients; the
  SDK mapping was verified by *reading* the installed packages, not over the network
  (README, same section; `docs/architecture.md`, *Production'a geçmeden önce açık kapılar*).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Concatenate the failed prefix with the replacement's output | Produces text no model wrote — duplicated openings, contradictory halves, invalid JSON — and is unreproducible because the seam exists only in the router (`router.stream` docstring). |
| Emit `StreamRestarted` *after* the replacement's first delta | The consumer would observe a spliced state for at least one frame. The ordering is claimed to be a mutation check: the module docstring states that "inverting the restart-before-delta order … makes named tests fail", and commit `88e600e` says each semantic is "backed by a test that fails when the guard is removed". **That mutation check is cited as recorded in those two places; it was not re-run for this ADR.** |
| Retry the same provider after partial output had already reached the consumer | Rejected in `router.stream`: once the consumer has seen text the event is a restart, not a retry, because the justification for a clean retry ("the attempt is indistinguishable from a fresh request") no longer holds. |
| Buffer always, never stream partials | Not taken — both modes exist and `buffered=False` is the default. **Rationale for that default is not recorded in the repo at the time of this ADR; inferred from the `stream(..., *, buffered: bool = False)` signature in `router.py`.** |

## Evidence

Source:

* `src/wozto_ai_reference/llm_gateway/router.py` — module docstring (the two load-bearing
  rules and the mutation checks), `FailoverRouter.stream` docstring and body (the
  restart-before-delta gate, the zero-delta restart debt, the exhausted-path restart, the
  `(buffered or not chunks)` retry condition), `_restart_event`.
* `src/wozto_ai_reference/llm_gateway/types.py` — `StreamRestarted` (`discarded_chars` is
  "exactly how many characters the consumer must throw away"), `StreamEnd` ("never the
  concatenation of a failed provider's partial text and the replacement's text"),
  `TextDelta` (each fragment tagged with its producing provider).
* `src/wozto_ai_reference/llm_gateway/ledger.py` — `AttemptRecord.discarded_chars`: in
  buffered mode the consumer never sees it, "the only place it is recorded is here".
* `README.md`, *Akış (stream) sözleşmesi* (7 numbered clauses) and *Bilinen sınırlar*.
* `docs/architecture.md`, *LLM gateway* section, clause (c).
* Commit `88e600e` message — "Failover only at request boundary… `StreamEnd.completion.text`
  is exactly one provider's text on every path (secondary-also-fails, savings mode,
  breaker-open, retry-within-provider)".

Tests (`tests/test_llm_gateway_stream.py`):
`test_restart_is_emitted_before_any_secondary_delta` ·
`test_final_text_is_exactly_one_providers_output` ·
`test_a_naive_concatenating_consumer_is_wrong_and_a_resetting_one_is_right` ·
`test_failure_before_the_first_delta_fails_over_transparently` ·
`test_partial_failure_does_not_retry_the_same_provider` ·
`test_clean_failure_before_output_still_uses_the_retry_budget` ·
`test_buffered_mode_hides_partial_output_entirely` ·
`test_buffered_discard_is_still_recorded_in_the_ledger` ·
`test_unbuffered_mode_emits_partial_output_as_it_arrives` ·
`test_buffered_mode_retries_the_same_provider_after_a_mid_stream_failure` ·
`test_savings_mode_stream_still_tells_the_consumer_to_discard`.

Numbers: this ADR states none of its own. The only quantity in the rule is
`discarded_chars`, asserted in `test_restart_is_emitted_before_any_secondary_delta` as
`len("".join(PRIMARY_CHUNKS))` — derived from the test's own fixture, not from a measurement.

## Related

* ADR 0002 — the ambiguous-outcome rule, the other load-bearing rule in `router.py`; both
  guards are checked on the failover path.
* ADR 0003 — `Usage` honesty for streamed answers (`StreamUsageReporter`).
* ADR 0004 — the attempt ledger, which is where a buffered discard and an abandoned stream
  leave their only trace.
