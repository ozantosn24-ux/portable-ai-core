# 0003 — Provider-neutral ports, optional SDKs, and a `Usage` that admits when it is guessing

## Status

Accepted — 2026-09-06.

## Context

The package describes itself as a cloud-neutral reference core
(`pyproject.toml`: *"Cloud-neutral reference core for tenant-safe Wozto AI applications"*).
Making two vendor SDKs mandatory dependencies would be the opposite of that — the wording in
`pyproject.toml`'s `llm` extra comment is exactly that: *"iki satıcı SDK'sını zorunlu
bağımlılık yapmak tam tersi olurdu."*

Three separate pressures land on the same design:

* **Shape.** A `Completion` from an Anthropic adapter and one from an OpenAI adapter must be
  the same object so that "the router, the ledger and the caller never branch on which
  provider answered" (`llm_gateway/types.py`, module docstring).
* **Dependencies.** The repo already has one optional-extra precedent (`embeddings`, kept out
  of the core because `torch` is ~200 MB+).
* **Honesty about numbers.** Token counts feed cost reports and quota decisions. An estimate
  that looks authoritative is worse than no number.

## Decision

### Ports stay provider-neutral, and the chat port stays separate from the RAG port

`ChatProvider` and `StreamUsageReporter` live in `llm_gateway/ports.py`, not in the
top-level `ports.py`. The module docstring records why: the repo's `ModelProvider` is a
*grounded answer* port bound to the RAG contract (retrieval hits, trace id, `ModelOutput`);
a chat provider is a lower layer with no notion of retrieval or citations, and "widening
`ModelProvider` to cover both would force every RAG adapter to grow chat-shaped parameters
it has no use for."

`ChatProvider.model` exists because "a streamed answer has no response object to read the
model from"; for `complete()` the adapter reports what the server echoed, "that is the
stronger source and the router prefers it". `ChatProvider.stream` is deliberately declared
without `async def`, because implementations are async generators and calling one returns an
iterator rather than a coroutine.

### `Usage.exact` — one estimate makes the sum an estimate

`Usage.exact` is `True` only when a provider returned the counts, and `Usage.__add__`
propagates it with `and`, not `or`: "a sum containing one estimate is an estimate. Without
this flag an approximate number silently becomes the input to a cost report or a quota
decision and looks authoritative" (`types.py`, `Usage` docstring; the `and` carries its own
inline comment).

Consequences of that choice through the rest of the gateway:

* `StreamUsageReporter` is an **optional** protocol: "a provider that cannot report usage
  must be able to stay silent rather than invent a number. Silence yields an inexact `Usage`;
  a guess would yield a wrong one wearing `exact=True`" (`llm_gateway/ports.py`).
* `router._stream_usage` falls back to a default `Usage()` (inexact, zeroes) when nobody
  counted, rather than deriving an estimate.
* `router._reported_stream_usage` keeps `None` and `Usage(0, 0)` apart: "the first is
  'nobody counted', the second is 'counted, and it was zero'".
* `StaticTemplateSavingsMode` sets `exact=True` on `Usage(0, 0)` — zero tokens *were* spent
  and that is a known number, not an estimate (`policy.py`).
* `Completion.attempts` allows `0` and the field comment says why: zero means no provider was
  ever called (savings-mode template with every breaker open), and `ge=1` "would record that
  case as '1 attempt made' — the one line where the ledger would be lying".

### SDKs are an optional extra, imported lazily

* `pyproject.toml` declares `llm = ["anthropic>=1.4,<2", "openai>=3.8,<4"]` under
  `[project.optional-dependencies]`, alongside `embeddings`, `mcp`, `auth` and `dev`.
* Lower bounds are **measured, not recalled**: the comment records `pip index versions
  <paket>` on 2026-09-05 returning anthropic 1.4.0 and openai 3.8.0 as latest.
* Upper bound is the next major, because both SDKs break across majors — "anthropic 1.x
  moved to httpx2 and REMOVED `temperature` from `messages.create` — measured on the
  installed 1.4.0 signature; openai 3.x made its own breaking change" — so an open upper
  bound means "the adapter one day silently sends the wrong parameter".
* `llm_gateway/__init__.py` states the invariant: "Importing this package never imports a
  provider SDK." `providers/__init__.py` deliberately does not re-export the SDK-backed
  adapters, because that import would run at package-import time.
* `_sdk_common.import_sdk` raises `ProviderDependencyMissing` **at construction time**, with
  a message naming the extra: "a deployment that is missing a dependency should break while it
  is being wired, not hours later under load when the primary provider goes down and the
  failover target turns out to be unimportable."

### Request-side errors do not count against the circuit breaker

`router._count_provider_failure` skips `NonRetryableError`. Its docstring: "`BadRequestError`
/ `ContentPolicyError` say nothing about the provider's health. Counting them opens the
circuit of a perfectly healthy provider, and the next unrelated caller is then fenced off
from it: one malformed prompt (or a batch of refused ones) takes the primary offline for
`open_seconds` while nothing is actually wrong with it. The breaker exists to fence a FAILING
PROVIDER, not a failing request."

An abandoned stream also leaves the breaker untouched — "the provider did not fail, the
consumer left" (`router.stream`, the `finally` block).

## Consequences

**Positive**

* Importing the adapter modules does not pull in a vendor SDK —
  `test_importing_the_adapter_modules_does_not_import_any_sdk` asserts that neither
  `"anthropic"` nor `"openai"` is in `sys.modules`.
* A missing SDK produces a message naming the extra to install, not a raw `ImportError`, and
  the test forces that path even on a machine where the package *is* installed by setting
  `sys.modules[name] = None`
  (`test_missing_sdk_names_the_extra_instead_of_leaking_an_import_error`, parametrised over
  both adapters).
* Cost and quota consumers can tell a measured number from an unmeasured one without asking
  the gateway how the number was produced.
* A malformed or refused prompt cannot fence off a healthy provider for other callers
  (`test_request_defects_do_not_open_the_circuit_of_a_healthy_provider`).

**Negative / not solved**

* **Pins need maintenance.** A closed upper bound means a new SDK major requires a measured
  bump, not a silent one; that is the intended cost, but it is a recurring one.
* **`_last_usage` is per-adapter mutable state.** The invariant is that no `await` sits
  between the write and the read; sharing one adapter instance across two concurrent streams
  breaks it and one stream reads the other's token count. README records this as held "by
  discipline, not by the type system".
* **`Usage.exact=False` is not itself a measurement.** It tells the consumer the number is
  unreliable; it does not say how wrong it is, and the package produces no estimate at all
  for an unreported stream.
* **The multi-choice merge in the OpenAI adapter is unreachable today** (`n` is never sent),
  so it is a forward-looking, untested branch (README, *Bilinen sınırlar*).
* **The SDK mapping was verified by reading the installed packages, not over the network.**
  No live provider call was made (README; commit `88e600e`).
* **The breaker rule depends on correct classification.** A provider defect misclassified as
  a `NonRetryableError` would never open the circuit.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Add chat to the existing `ModelProvider` port | Would force every RAG adapter to grow chat-shaped parameters it has no use for (`llm_gateway/ports.py` module docstring). |
| Make `anthropic` / `openai` core dependencies | Directly contradicts the package's stated goal of a provider-neutral core (`pyproject.toml`, `llm` extra comment). |
| Re-export the SDK adapters from `providers/__init__.py` | The import would run at `import wozto_ai_reference.llm_gateway` time "and drag the optional dependency question into every import of the package" (`providers/__init__.py`). |
| Raise the missing-SDK error on the first request instead of at construction | The deployment would break under load, at the worst possible moment — when the failover target is first needed (`_sdk_common.import_sdk`). |
| Open upper version bounds | Both SDKs break across majors; an open bound means eventually sending a parameter the API rejects (`pyproject.toml` comment, measured against installed 1.4.0). |
| `exact = self.exact or other.exact` in `Usage.__add__` | Would let a number that is not exact enter a billing or quota decision wearing the `exact` label (inline comment on that line). |
| Have the router estimate tokens when a provider reports none | A fabricated estimate written with `exact=True` would be mistaken for a measurement by a cost report (`router._stream_usage` comment); silence is the honest answer. |
| Make `StreamUsageReporter` mandatory on `ChatProvider` | A provider that cannot count would have to invent a number (`llm_gateway/ports.py`). |
| Count request-side errors as provider failures | One malformed prompt would take a healthy primary offline for `open_seconds` for every unrelated caller (`router._count_provider_failure`). |

## Evidence

Source:

* `src/wozto_ai_reference/llm_gateway/ports.py` — the whole module docstring, `ChatProvider`
  (`provider_id`, `model`, `stream` non-`async def` note), `StreamUsageReporter`.
* `src/wozto_ai_reference/ports.py` — `ModelProvider`, `EmbeddingProvider`,
  `SearchProvider` and the rest of the RAG port surface this one deliberately does not join.
* `src/wozto_ai_reference/llm_gateway/types.py` — module docstring, `Usage` (`exact`,
  `__add__`), `Completion` (`attempts` with `ge=0`, `request_id`).
* `src/wozto_ai_reference/llm_gateway/router.py` — `_stream_usage`,
  `_reported_stream_usage`, `_count_provider_failure`, and the `finally` block that keeps the
  breaker out of abandoned attempts.
* `src/wozto_ai_reference/llm_gateway/policy.py` — `CircuitBreaker` (states and thresholds),
  `StaticTemplateSavingsMode` (`Usage(0, 0, exact=True)`).
* `src/wozto_ai_reference/llm_gateway/providers/_sdk_common.py` — `import_sdk`,
  `retry_after_seconds` (header precedence read off `anthropic==1.4.0`'s own parser).
* `src/wozto_ai_reference/llm_gateway/providers/__init__.py` and
  `src/wozto_ai_reference/llm_gateway/__init__.py` — the no-SDK-at-import rule.
* `src/wozto_ai_reference/llm_gateway/providers/anthropic_adapter.py` — module docstring's
  three measured items against `anthropic==1.4.0` (no `temperature` on `messages.create`; no
  `text_stream` on the stream helper; a refusal is HTTP 200 with `stop_reason == "refusal"`).
* `pyproject.toml` — `[project.optional-dependencies]` `llm`, and the comment recording the
  2026-09-05 `pip index versions` measurement.
* `README.md`, *LLM gateway* intro, the ⛔ paragraph on the circuit breaker, and
  *Bilinen sınırlar*.
* `docs/architecture.md`, *LLM gateway* clause (a) and the closing sentence on the breaker;
  the ports table row for `ChatProvider`.

Tests:

* `tests/test_llm_gateway_adapters.py` —
  `test_importing_the_adapter_modules_does_not_import_any_sdk` ·
  `test_missing_sdk_names_the_extra_instead_of_leaking_an_import_error` ·
  `test_anthropic_does_not_send_temperature_by_default` ·
  `test_anthropic_sends_temperature_only_when_explicitly_enabled` ·
  `test_anthropic_usage_is_marked_exact` ·
  `test_openai_usage_is_read_from_prompt_and_completion_tokens` ·
  `test_openai_stream_requests_usage_and_captures_it` ·
  `test_retry_after_ms_wins_over_the_coarser_retry_after_seconds`.
* `tests/test_llm_gateway_policy.py` — `test_one_estimate_makes_the_whole_sum_an_estimate` ·
  `test_usage_defaults_to_inexact` · the `CircuitBreaker` state-machine tests.
* `tests/test_llm_gateway_router.py` —
  `test_request_defects_do_not_open_the_circuit_of_a_healthy_provider` ·
  `test_breaker_opens_and_the_primary_is_not_called_while_open` ·
  `test_exact_usage_from_the_provider_survives_to_the_completion` ·
  `test_savings_mode_serves_a_template_instead_of_raising` ·
  `test_two_providers_may_not_share_a_provider_id`.
* `tests/test_llm_gateway_stream.py` — `test_stream_usage_is_exact_when_the_provider_reported_it` ·
  `test_stream_usage_is_inexact_when_nobody_counted`.

Numbers in this ADR and where each comes from: `anthropic 1.4.0` / `openai 3.8.0` — the
`pip index versions` run on 2026-09-05 recorded in `pyproject.toml`'s `llm` comment;
`~200 MB+` for `torch` — the `embeddings` extra comment in the same file. No number here was
produced for this ADR.

## Related

* ADR 0002 — the error taxonomy these adapters map into, and why request-side defects are
  raised rather than retried.
* ADR 0004 — the ledger row is where `Usage` and `exact` end up being read after an incident.
* `docs/architecture.md` port table — the adapter roster these ports are meant to admit.
