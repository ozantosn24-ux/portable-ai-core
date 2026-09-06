# 0007 — Tenant and ACL are enforced inside the SQL query; identity defaults are fail-closed

## Status

Accepted — 2026-09-06. (Pre-existing decision, recorded here retrospectively; it predates the
LLM gateway and the deployment drill.)

## Context

`docs/architecture.md`, *Güven sınırları*, states the two premises this ADR rests on:

1. "İstemcinin tenant ve rol beyanı production'da güvenilir değildir. Local header adapter
   yalnız geliştirme/test içindir ve varsayılan olarak kapalıdır." — a client's own claim about
   its tenant and roles is not trustworthy in production; the local header adapter is for
   development and test only and is off by default.
3. "Search adapter tenant/ACL filtresi uygular. `QueryService` aynı kontrolü ikinci kez yapar;
   hatalı veya ele geçirilmiş adapter'ın cross-tenant context'i modele taşımasını engeller."
   — the search adapter filters, and the service checks again, so a buggy or compromised
   adapter cannot carry cross-tenant context into the model.

`service.py`'s module docstring names the shape: *"Provider-neutral query orchestration with
defense-in-depth authorization."*

## Decision

### The authorization filter lives inside the query, before ranking

`PgVectorStore.search` builds an `authorized` CTE whose `WHERE` clause admits only rows the
principal may see. Unauthorized rows never reach the ranking stages that follow — the
`scaled` CTE's per-query normalisation and the final weighted `ORDER BY`. (The raw
`vector_score` / `lexical_score` expressions sit in the same CTE's select list as this
`WHERE`, so the precise claim is the one `docs/architecture.md` clause 14 makes — the filter
is applied *before ranking* — not that no arithmetic touches a filtered row.)

```sql
WHERE tenant_id = %s
  AND (cardinality(acl_roles) = 0 OR acl_roles && %s::text[])
  AND embedding_model = %s
```

Both the tenant and the principal's roles arrive as bound parameters (`principal.tenant_id`,
`sorted(principal.roles)`), never as interpolated text. `docs/architecture.md` clause 14 states
the ordering explicitly: "PostgreSQL tenant ve rol filtresini ranking'den önce uygular."

The third condition belongs to a different concern but shares the mechanism: rows embedded in a
different embedding space are excluded, and their presence is then raised as
`EmbeddingSpaceMismatch` rather than returned as an empty result — "sessizce bos sonuc donmek
yerine bunu yukseltiyoruz: bos sonuc 'eslesme yok' diye okunur ve gercek sebep (reindex
gerekiyor) aylarca gorunmez kalir."

### The service re-checks, rather than trusting the adapter

`QueryService` applies the same authorization a second time (`docs/architecture.md` clauses 3
and 14). The point is not redundancy for its own sake: it bounds the blast radius of a wrong or
compromised `SearchProvider`.

### Identity is off unless something trusted is wired

* `LocalHeaderIdentityProvider.__init__` takes `enabled: bool = False`; its docstring says
  "Explicitly enabled local-only identity adapter; never a production trust boundary." When
  disabled, `resolve` raises `IdentityUnavailable` (`adapters.py`).
* `api.py` (as committed at `b407058`, unchanged by `88e600e` and `3b3e238`) turns it on only
  from an explicitly named environment variable,
  `_LOCAL_IDENTITY_ENV = "WOZTO_REFERENCE_ALLOW_INSECURE_HEADERS"`, compared against `"1"`.
* `/ready` returns **503** with `{"status": "not_ready", "reason": "identity_disabled"}`
  whenever `resolved_identity.ready` is false; `IdentityUnavailable` during a query becomes a
  503 `"Trusted identity provider is unavailable"`.

That default has a visible downstream cost, and it was accepted rather than worked around: the
deployment drill's app healthcheck uses `/health`, because "`/ready` deliberately answers 503
while no trusted identity provider is wired (see api.py)"
(`deploy/compose-drill/compose.yaml`, app service comment).

### The same boundary is carried across the MCP protocol

`README.md` has a dedicated section, *MCP sunucusu — yetki sınırını protokole taşımak*, with a
subsection reporting the measured result that the boundary holds in two layers. The
corresponding tests are `test_other_tenant_document_NEVER_returned` and
`test_acl_role_gates_restricted_document`.

## Consequences

**Positive**

* A cross-tenant row cannot reach the ranking stage, let alone the model, and the parameters
  that decide it are asserted at the SQL level by
  `test_search_pushes_tenant_and_acl_filter_parameters_into_sql`.
* Two independent layers must both fail before cross-tenant context reaches a model.
* A deployment that forgets to wire identity does not silently accept header claims; it
  answers 503 and says which precondition is missing.
* An empty ACL list means "no role restriction" (`cardinality(acl_roles) = 0`), so authorship
  of a document does not have to enumerate every reader.

**Negative / not solved**

* **The verified-claim identity adapter is still missing.** `docs/architecture.md`,
  *Production'a geçmeden önce açık kapılar*, lists as still open: "JWT doğrulayan identity
  adapter ve tenant'ın doğrulanmış claim'den türetilmesi". Everything above assumes the
  `Principal` handed to it is trustworthy; producing a trustworthy one is out of scope for
  this ADR and is why 0008 is reserved.
* **Prompt-injection and cross-tenant negative tests are listed as open** in the same section
  — the existing tests are positive-and-negative unit cases, not an adversarial suite.
* **The real database path is only covered by an opt-in test.**
  `tests/test_pgvector_integration.py` is skipped unless
  `WOZTO_REFERENCE_TEST_DATABASE_URL` is set. `pgvector_store.py` carries a comment about
  exactly what that skip cost once: a `str.format` placeholder bug in the DDL went unnoticed
  because "bu kodu çalıştıran tek test `WOZTO_REFERENCE_TEST_DATABASE_URL` yokluğunda
  ATLANIYORDU. Yani `initialize()` bugüne kadar hiç koşmamıştı."
* **`/ready` returning 503 by default is a real operational cost**, absorbed by the drill by
  probing `/health` instead — which means the drill's healthcheck does not assert readiness.
* **Defense in depth costs a second pass** over the same rows in the service layer, and keeps
  the authorization rule expressed in two places that must stay in agreement.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Filter tenant/ACL in Python after retrieving ranked rows | The filter would run after ranking, so another tenant's rows would already have competed for the top-k slots. `docs/architecture.md` clause 14 fixes the order at the database. |
| Rely on the search adapter alone | Clause 3: the service repeats the check specifically so that a wrong or compromised adapter cannot carry cross-tenant context into the model. |
| Interpolate the tenant id and roles into the SQL text | Not done anywhere in `search`; both are bound parameters. The one value that *does* enter DDL literally (`text_search_config`) is whitelisted first, with the comment "BEYAZ LISTE SART: bu deger DDL'e (generated column) LITERAL olarak giriyor, yer tutucuyla gecemez ⇒ dogrulanmazsa enjeksiyon yuzeyi olur." |
| Have `LocalHeaderIdentityProvider` default to enabled for convenience | It is "never a production trust boundary" (`adapters.py`); defaulting to on would make a forgotten configuration indistinguishable from a deliberate one. |
| Let `/ready` return 200 and fail only on the first query | Failing closed at readiness is what makes the missing precondition visible to an orchestrator; the drill absorbed the cost rather than relaxing it (`deploy/compose-drill/compose.yaml`). |
| Return an empty result set when stored vectors are in another embedding space | Rejected in `EmbeddingSpaceMismatch`'s docstring: an empty result reads as "no match" and hides the real cause for months. |

## Evidence

Source:

* `src/wozto_ai_reference/pgvector_store.py` — module docstring ("PostgreSQL/pgvector document
  store with tenant and ACL filtering in SQL"), the `authorized` CTE `WHERE` clause in
  `search`, the bound-parameter tuple (`principal.tenant_id`, `sorted(principal.roles)`,
  `self._space_id`), `EmbeddingSpaceMismatch`, the whitelist on `text_search_config`, and the
  DDL comment recording that `initialize()` had never run under the skipped integration test.
* `src/wozto_ai_reference/adapters.py` — `LocalHeaderIdentityProvider` (`enabled: bool = False`,
  `ready`, `IdentityUnavailable` on a disabled resolve).
* `src/wozto_ai_reference/api.py` (committed state at `b407058`) — `_LOCAL_IDENTITY_ENV`,
  the `identity or LocalHeaderIdentityProvider(enabled=allow_insecure_identity)` composition,
  the `/ready` 503 branch, and the `IdentityUnavailable` → 503 handler.
* `src/wozto_ai_reference/ports.py` — `IdentityUnavailable`, `SearchProvider`.
* `src/wozto_ai_reference/service.py` — module docstring naming defense-in-depth authorization.
* `docs/architecture.md` — *Güven sınırları* clauses 1, 3 and 14; the `IdentityProvider`
  row of the ports table ("açıkça etkinleştirilen local header adapter"); *Production'a
  geçmeden önce açık kapılar*.
* `README.md` — *MCP sunucusu — yetki sınırını protokole taşımak*, including
  *⭐ Ölçülen sonuç: sınır İKİ katmanda korunuyor*.
* `deploy/compose-drill/compose.yaml` — the app-service comment explaining why liveness probes
  `/health`.

Tests:

* `tests/test_pgvector_store.py` — `test_search_pushes_tenant_and_acl_filter_parameters_into_sql`.
* `tests/test_pgvector_integration.py` — `test_pgvector_round_trip_enforces_tenant_and_acl`
  (skipped unless `WOZTO_REFERENCE_TEST_DATABASE_URL` is set).
* `tests/test_service.py` — `test_search_and_service_enforce_tenant_and_acl` ·
  `test_authorized_role_can_retrieve_restricted_document`.
* `tests/test_api.py` — `test_health_is_available_but_readiness_fails_closed_without_identity` ·
  `test_local_query_requires_explicit_identity_and_returns_authorized_citation` ·
  `test_local_query_rejects_missing_identity_headers` ·
  `test_query_refuses_identity_smuggling_in_request_body`.
* `tests/test_mcp_server.py` — `test_other_tenant_document_NEVER_returned` ·
  `test_acl_role_gates_restricted_document`.

Numbers: this ADR states none. The only literals it quotes are the HTTP status `503` and the
environment variable name `WOZTO_REFERENCE_ALLOW_INSECURE_HEADERS`, both read from `api.py`.

**Not verified for this ADR:** the original rationale for this decision is not recorded as a
decision record anywhere; it is reconstructed from the trust-boundary list in
`docs/architecture.md` and from the docstrings and tests cited above. No commit message in
`git log` states it as a decision.

## Related

* **ADR 0008 (pending)** — OIDC login and resource-level authorization. The `identity/` package
  and the `auth` extra in `pyproject.toml` exist in the working tree as an in-flight spike; the
  ADR will be written when it lands. Until then, the open item "JWT doğrulayan identity adapter"
  in `docs/architecture.md` stands.
* ADR 0006 — the drill's app healthcheck decision, which is a direct consequence of `/ready`
  failing closed.
