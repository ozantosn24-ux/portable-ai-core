# 0004 — The append-only attempt ledger is the record of truth

## Status

Accepted — 2026-09-06.

## Context

After a duplicated charge or a surprise bill, someone has to reconstruct what the router
actually did. The question is not "how is the system doing" but *"what did the router try,
in what order, and what came back?"* — and `llm_gateway/ledger.py`'s module docstring records
that the existing `TelemetryProvider` port was checked first and does not fit:

> `TelemetryProvider.record(TelemetryEvent)` is a tenant-scoped, trace-keyed event sink whose
> payload is a flat `dict[str, str | int | float | bool]` — it models "something notable
> happened", and an implementation is free to sample, batch or drop. The attempt ledger
> answers a different question … It needs a fixed schema with a nested `Usage`, strict
> ordering, and durability across a restart, because it is the artifact you read after a
> duplicated charge or a surprise bill.

`docs/architecture.md` states the coverage rule with no exception:
**"sağlayıcı çağrıldıysa satırı vardır"** — if the provider was called, there is a row.

## Decision

1. **Append-only.** "A row is written the moment an attempt *resolves*, and is never
   revisited. If a later attempt could rewrite an earlier row, the one thing the ledger
   exists to prove — that the request went out twice — becomes the thing it can hide."
2. **A separate port from telemetry.** `AttemptLedger` is its own `Protocol` with a fixed
   `AttemptRecord` schema. Merging it into `TelemetryProvider` "would force the ledger's
   guarantees onto every telemetry backend" (module docstring).
3. **Two clocks, two jobs.** `AttemptRecord.ts` is an ISO-8601 UTC string written from the
   injected **wall clock**; `latency_ms` is measured from the injected **monotonic** clock.
   The comment in `policy.py` and the field comment in `ledger.py` both spell out the failure
   in each direction: a monotonic clock has an arbitrary origin (recorded on the development
   machine as ~229779.27 s), so using it for `ts` drops every row to 1970; a wall clock can
   jump backwards on an NTP correction, so using it for duration produces negative latencies.
   `router._iso_utc` explains the string-and-UTC choice: the ledger is read by a human after
   an incident and compared against a provider's dashboard, and "a bare float forces every
   reader to guess an epoch and a zone".
4. **One row per attempt, including the attempts nobody would think to log.** The
   `AttemptOutcome` literal is
   `"ok" | "error" | "abandoned" | "skipped_open_circuit" | "savings_mode"`:
   * `abandoned` — the provider *was* called but neither success nor failure ended the
     attempt, because the consumer left the stream (client disconnect, `break`, cancellation).
     Written from a `finally` guarded by a `resolved` flag. The outcome needs its own name:
     "writing `error` blames the provider and pollutes the circuit-breaker statistics; writing
     nothing erases a call that really happened (and may have been billed)". The breaker is
     deliberately untouched on this path.
   * `skipped_open_circuit` — `attempt=0`, the provider was never called. These rows are
     excluded when counting attempts but still written, because "nothing else shows that the
     breaker bit".
5. **Two character counters, kept apart.** `discarded_chars` is text thrown away because of a
   restart (ADR 0001); `delivered_chars` is what the provider produced before an abandoning
   consumer walked away. The field comment states that merging them "would confuse the two
   events the ledger exists to distinguish".
6. **The original exception type is recorded, not the wrapper.** `router._record` is called
   with `error_class=type(exc).__name__`: "whoever reads the ledger should see what actually
   blew up, not `UnclassifiedProviderError`".
7. **`Completion.request_id` is the key back into the ledger.** The field comment: a caller
   who sees `attempts=3` and cannot ask "which three rows?" gets nothing out of the ledger
   existing. The router stamps it on the way out.
8. **JSONL on disk, re-opened per record.** `JsonlAttemptLedger` opens the file in append
   mode for every write: "the file stays readable and rotatable by other tooling while the
   process runs, and a crash cannot lose a record that a buffered handle had not flushed. The
   cost is one `open()` per attempt, which is nothing next to a model call."
9. **Bytes are written by us, not by the OS.** `newline=""`, `sort_keys=True`,
   `ensure_ascii=False` — Python's text mode rewrites `\n` into `\r\n` on Windows, so "the
   *same* ledger written on two machines carries different bytes", stable field order makes two
   rows diffable, and non-ASCII text stays readable instead of becoming `\u` escapes.

## Consequences

**Positive**

* **Row count ≥ attempt count, and the difference is itself information.** `Completion.attempts`
  counts only real provider calls — `router` increments it after `breaker.allows_request()`
  passes — while `skipped_open_circuit` rows are written without being counted
  (`AttemptRecord.attempt` comment: "Bu satırlar denemeleri SAYARKEN hariç tutulur ama
  yazılır"). The two numbers are equal only when no breaker-skip row was written; the
  invariant that always holds is `len(ledger.provider_attempts()) == attempts`, since
  `InMemoryAttemptLedger.provider_attempts()` filters exactly those rows out.
  `test_ledger_line_count_equals_attempt_count` exercises the equal case (3 attempts, 3 rows,
  `provider_attempts()` also 3, no breaker involved);
  `test_breaker_opens_and_the_primary_is_not_called_while_open` exercises the unequal one —
  its second call skips the fenced primary and answers from the secondary, so one attempt
  produces two rows, and the test asserts a `skipped_open_circuit` row is present.
  `Completion.attempts` is meant to be comparable against the ledger via `request_id`
  (`types.Completion` field comments).
* A consumer that disconnects, a request skipped by an open breaker, an adapter that raised
  something unmapped, and a savings-mode template all leave a row. Each of those was a path
  that previously left no trace (`docs/architecture.md`: "ikisi de daha önce hiç iz bırakmadan
  geçebiliyordu").
* The file is diffable and comparable across machines, and appending across process restarts
  is tested (`test_jsonl_ledger_is_append_only_across_runs`,
  `test_jsonl_ledger_writes_lf_only_line_endings`).

**Negative / not solved**

* **One `open()` per attempt.** Explicitly accepted as cheap next to a model call — but it is
  still a syscall per row, and nothing here batches or rotates.
* **No rotation, retention, redaction or query layer.** `JsonlAttemptLedger` writes; reading
  is `read_all()` into memory. Anything larger is the operator's problem.
* **No cross-process ordering guarantee is claimed.** Rows are appended in the writing
  process's call order; `InMemoryAttemptLedger` "holds records in call order". Concurrent
  writers are not discussed anywhere in the module.
* **`AttemptRecord` records what the router knew, not what the provider did.** An `abandoned`
  row says the provider was called and may have been billed; it cannot say whether it was.
* **The ledger does not replace telemetry.** The two are meant to coexist — "telemetry for
  aggregate dashboards, this for reconstruction" — so a deployment that wants dashboards still
  needs a `TelemetryProvider`, and `docs/architecture.md` lists OpenTelemetry trace/latency/
  token/cost measurement as still open.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Reuse `TelemetryProvider` | Checked first and rejected: it is free to sample, batch or drop, its payload is flat, and it models "something notable happened" rather than a reconstructable sequence. Merging would force the ledger's guarantees onto every telemetry backend (`ledger.py` module docstring). |
| Let a later attempt update the earlier row (mutable ledger) | "The one thing the ledger exists to prove — that the request went out twice — becomes the thing it can hide" (same docstring). |
| Skip rows for attempts nobody consumed (abandoned streams) | Erases a call that really happened and may have been billed (`AttemptOutcome` comment). |
| Record an abandoned attempt as `error` | Blames the provider and pollutes circuit-breaker statistics (same comment). |
| Omit `skipped_open_circuit` rows because no call was made | Nothing else in the system shows that the breaker bit (`AttemptRecord.attempt` comment). |
| One character counter for both restart-discarded and consumer-delivered text | Confuses the two events the ledger exists to distinguish (`delivered_chars` comment). |
| Store `ts` as an epoch float, or from the monotonic clock | A bare float forces every reader to guess an epoch and a zone (`router._iso_utc`); the monotonic clock would drop every row to 1970 (`AttemptRecord.ts` comment). |
| Hold one open file handle | A crash loses unflushed records and the file stays locked against rotation (`JsonlAttemptLedger` docstring). |
| Let the OS choose the line terminator | The same ledger then carries different bytes on two machines, breaking any tool that diffs, hashes or measures a line (`jsonl_ledger.py`, lesson 1). |

## Evidence

Source:

* `src/wozto_ai_reference/llm_gateway/ledger.py` — module docstring (*Why not reuse
  `TelemetryProvider`*, *Why append-only*), `AttemptOutcome`, `AttemptRecord` (all field
  comments: `ts`, `attempt`, `discarded_chars`, `delivered_chars`), `AttemptLedger`,
  `InMemoryAttemptLedger`, `JsonlAttemptLedger`.
* `src/wozto_ai_reference/llm_gateway/router.py` — `_record`, `_iso_utc`, the `finally`
  block writing `outcome="abandoned"`, the `skipped_open_circuit` writes in both `complete`
  and `stream`, the `savings_mode` write in `_exhausted`, and the `error_class=type(exc).__name__`
  comment.
* `src/wozto_ai_reference/llm_gateway/policy.py` — the two-clock comment defining `Clock`
  (monotonic, arbitrary origin recorded as ~229779.27 s on this machine) and `WallClock`.
* `src/wozto_ai_reference/llm_gateway/types.py` — `Completion.attempts` (why `ge=0`) and
  `Completion.request_id` (why the answer must carry the ledger key).
* `src/wozto_ai_reference/jsonl_ledger.py` — the three shared lessons (`newline=""`,
  re-open per record, `sort_keys` + `ensure_ascii=False`) and the ⛔ rule that the module holds
  no schema.
  **Status caveat: this file exists in the working tree but is in neither `88e600e` nor
  `3b3e238`; `git status` reports it untracked as of 2026-09-06. The committed
  `JsonlAttemptLedger.append` still inlines the same `newline=""` write. This ADR cites the
  extraction as pending review, not as merged.**
* `README.md`, *Defter (`AttemptLedger`)*.
* `docs/architecture.md`, *LLM gateway* — the no-exception coverage rule.
* Commit `88e600e` message: "Append-only JSONL attempt ledger: one row per attempt, including
  abandoned streams; ISO-8601 UTC wall-clock ts, latency from the monotonic clock; Completion
  carries request_id."

Tests:

* `tests/test_llm_gateway_router.py` — `test_ledger_line_count_equals_attempt_count`
  (rows == attempts when no breaker-skip row exists) ·
  `test_breaker_opens_and_the_primary_is_not_called_while_open`
  (one attempt, two rows, a `skipped_open_circuit` row asserted present) ·
  `test_jsonl_ledger_is_append_only_across_runs` ·
  `test_ledger_ts_comes_from_the_wall_clock_while_latency_stays_monotonic` ·
  `test_completion_carries_the_request_id_that_keys_its_ledger_rows` ·
  `test_jsonl_ledger_writes_lf_only_line_endings` ·
  `test_unmapped_adapter_exception_is_ledgered_and_failed_over` ·
  `test_savings_mode_serves_a_template_instead_of_raising`.
* `tests/test_llm_gateway_stream.py` — `test_a_consumer_that_walks_away_still_leaves_one_ledger_row` ·
  `test_a_cancelled_consumer_task_also_leaves_an_abandoned_row` ·
  `test_a_resolved_attempt_the_consumer_leaves_early_is_not_marked_abandoned` ·
  `test_buffered_discard_is_still_recorded_in_the_ledger` ·
  `test_unmapped_stream_exception_is_ledgered_and_failed_over` ·
  `test_stream_end_completion_carries_the_request_id`.

Numbers: the only figure quoted is the monotonic clock origin **~229779.27 s**, which is
copied from the comments in `policy.py` and `ledger.py` and was recorded on the development
machine; it is an illustration of the arbitrary origin, not a measurement made for this ADR.

**Not verified for this ADR:** the task brief framed this decision partly as
*"checkpoint ≠ ledger"*. No such distinction is written anywhere in this repository — the word
*checkpoint* appears only as a project-milestone label (`README.md` headings,
`api.py` module docstring). The recorded distinction is *ledger ≠ telemetry*, and that is what
this ADR documents.

## Related

* ADR 0001 — `discarded_chars`, and the buffered-mode discard whose only record is a ledger row.
* ADR 0002 — every raise on the ambiguous branch also leaves a row, with the original
  exception's class name.
* ADR 0003 — `Usage` and its `exact` flag as stored in `AttemptRecord.usage`.
