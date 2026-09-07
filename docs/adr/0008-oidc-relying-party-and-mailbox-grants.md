# 0008 — OIDC login and resource-level authorization

## Status

Accepted 2026-09-06. Reserved as "Pending" in the index since 0007; this record fills it.

## Context

The package had exactly one identity adapter: `LocalHeaderIdentityProvider`, which trusts
`X-Tenant-ID` / `X-User-ID` / `X-Roles` and is off by default because those headers are not
a trust boundary. ADR 0007 recorded that default and named the open gap: *"a JWT-verifying
identity adapter and deriving the tenant from a verified claim"*.

Closing that gap raises three questions that are usually collapsed into one, and collapsing
them is how authorization bugs get built:

1. **Who is this?** — answerable only from a signature the caller cannot forge.
2. **Is this browser still that person?** — a different question, answered by session state,
   with its own failure modes (fixation, replay after logout, CSRF).
3. **May this person do this to this resource?** — a question the IdP cannot answer at all.

## Decision

### 1. `httpx` + `joserfc`, not Authlib

The network surface is three requests: discovery `GET`, JWKS `GET`, token `POST`. Authlib
would bring an OAuth *framework* — multiple grant types, its own state storage, a client
registry — for the sake of one POST, and its state store would be a second source of truth
next to the server-side sessions this design already needs.

The decisive reason is narrower. **The algorithm allowlist is checked at the call site, and
before any key lookup.** `joserfc.jwt.decode(..., algorithms=[...])` demands the list
explicitly, and `oidc.py` additionally validates the JWS header `alg` against that list
*before* it touches the JWKS. The `alg: none` and HS-signed-with-an-RSA-public-key
confusion families therefore close before the signature code runs at all — not because a
library defaults well, but because the token never reaches key selection.

That claim is measured, not asserted:
`test_algorithm_allowlist_is_checked_before_any_key_lookup` configures the RP for `ES256`
against an RS256-signing IdP and asserts the JWKS endpoint was **never requested**
(`"jwks" not in idp.request_counts`) while the token endpoint *was* (count 1). If key
lookup happened first, that counter would be 1.

Honest cost: the JWKS cache, the discovery cache and every claim check are hand-written, so
they are this package's responsibility to test rather than a vendor's.

### 2. `resolve()` fails closed

`IdentityProvider.resolve(tenant_id=…, user_id=…, roles=…)` is a *header* shape. Under OIDC
the principal comes from a validated ID token plus a server-side session, so
`OidcIdentityProvider.resolve()` raises `IdentityUnavailable` and never reads those
arguments. The port is still implemented so `/ready` keeps its meaning — "is a trusted
provider wired", not "who is on this request".

**Known consequence, accepted deliberately:** with the switch on, `POST /query` answers 503
for everyone, because `/query` still uses the header path. Binding `/query` to the session
would change an existing route's contract; that was declined. The limitation is pinned by
`test_query_is_503_under_the_oidc_switch_known_limitation` so it cannot drift silently.

### 3. Sessions are server-side; the cookie is an opaque id

The cookie carries only a signed, opaque session id. Authority lives in the store, because
a self-contained cookie stays valid after the server has decided the session is over —
logout would not be logout. Login **rotates the id and deletes the old record** (both
halves, or it is not a fixation defence). CSRF is per-session and checked *before*
authorization, so an unevaluated request never becomes a decision.

### 4. The grant table resolves the mailbox — the request never does

`authorize(principal, resource, action)` looks the grant up **by principal** and then uses
`grant.mailbox_id`. The value the request named is only ever a lookup key; it survives in
the ledger as `requested_mailbox_id`, never as the decision's target. A principal who owns
one mailbox has no rights on another, and holding an IdP role grants nothing by itself:
roles are mapped into `Principal.roles` and then play no part in the mailbox decision.

⚠️ **This invariant was, for a while, tested by a fixture that could not fail.**
`InMemoryGrantTable.grant_for` is an exact dict lookup, so `requested` and
`grant.mailbox_id` were always the same string — replacing one with the other changed
nothing observable (measured at the time: the whole suite stayed green under the mutation; the
suite has grown since, so the count is not pinned). It is now measured with
`NormalisingGrantTable`, a case-insensitive double where `SALES-A` resolves to a grant
whose id is `sales-a`, so the two sources are finally distinguishable. *Testing an
invariant with a fixture whose two sides coincide is not testing it.*

### 5. `owner`/`delegate` ⊇ `manager_view`

`manager_view` means "summary **only**", not "summary is reserved for managers". A mailbox
owner who cannot see a summary of their own mailbox is a fault, not a security property, so
`owner` and `delegate` hold `{read, draft, send, view_summary}` and the table is a
superset relation.

### 6. One append-only ledger row per decision, written before the decision returns

Allow and deny both write exactly one row, inside `authorize()` — not at the call site,
because the path that most needs a record (an early deny) is the one a caller would forget.
The row is written **before** the decision is returned, which makes an unwritable ledger
fail the request instead of producing an unlogged allow. That ordering was exercised for
real in the drill (`PermissionError` on a root-owned volume produced a 500, never a 200)
and is now pinned by `test_an_unwritable_ledger_fails_the_request_never_silently_allows`.

Writing mechanics are shared with `llm_gateway`'s attempt ledger through
`jsonl_ledger` (`newline=""`, one `open()` per row, sorted keys). **The schema is not
shared**: sharing how to write must not become sharing what it means.

### 7. Third-party dependencies are optional and lazy

`httpx` and `joserfc` live in an `auth` extra and are imported at point of use.
`api.py` mounts the router only behind `WOZTO_REFERENCE_OIDC_ENABLED=1`, and every required
setting — including the ledger path — must be present at startup or the app refuses to
build. Missing configuration is never a silent downgrade.

## Consequences

* Swapping Keycloak for Entra ID is mostly configuration — six `OidcConfig` fields — with
  two real code gaps documented in `docs/identity-entra-migration.md`: certificate
  credentials (`private_key_jwt`) and multi-tenant issuer/`tid` validation.
* The default `InMemorySessionStore` loses every session on restart and cannot be shared
  across replicas. `SessionStore` is a Protocol so a Redis/Postgres store can replace it;
  no such store ships here.
* No token refresh and no back-channel logout: a logout at the IdP does not reach this
  application. Sessions end on their own TTL.
* Group **overage** in Entra can make the roles claim absent entirely. That fails *closed*
  here (no roles, and grants decide anyway) but it fails **silently**; the migration note
  records the detection options.
* Non-ASCII input reaching a credential comparison must not raise. `compare_digest` on
  `str` raises `TypeError` above U+007F and Starlette latin-1-decodes headers, so all five
  comparison sites go through `_support.constant_time_equals`, which encodes to bytes first.
  Without it a single `\xff` byte in a cookie turns a 401 into a 500.

## Evidence

| Claim | Where it is measured |
|---|---|
| `alg` allowlist precedes key lookup | `test_algorithm_allowlist_is_checked_before_any_key_lookup` (JWKS request count 0) |
| One JWKS refetch on unknown `kid`, then reject | `test_unknown_kid_triggers_exactly_one_refetch_then_rejects` (+ rotation positive control) |
| Bounded skew both ways | `test_bounded_skew_accepts_a_token_that_expired_inside_the_window`, `test_token_expired_beyond_the_skew_window_is_rejected` |
| Discovery issuer must match | `test_discovery_issuer_must_match_the_configured_issuer` (and the 404 case kept separate) |
| Session rotation and server-side logout | `test_login_rotates_the_session_id_and_deletes_the_old_record`, `test_logout_invalidates_server_side_so_the_old_cookie_is_dead` |
| Mailbox comes from the grant, not the request | `test_decision_carries_the_grants_mailbox_id_not_the_requested_spelling` |
| Ledger row per decision; unwritable ledger fails closed | `test_allow_and_deny_each_write_exactly_one_row`, `test_an_unwritable_ledger_fails_the_request_never_silently_allows` |
| Non-ASCII never produces a 500 | `tests/test_identity_hostile_input.py` |
| End to end against a real IdP | [`S2-DRILL-2026-09-06.md`](../../deploy/compose-drill/S2-DRILL-2026-09-06.md) §4–§5 — three principals, 8 ledger rows, append-only across a restart |

## Related

* [0003](0003-provider-neutral-ports-and-usage-honesty.md) — optional dependencies behind extras.
* [0004](0004-append-only-attempt-ledger.md) — the append-only ledger discipline this reuses.
* [0007](0007-tenant-acl-in-sql-and-fail-closed-identity.md) — the fail-closed identity default this extends.
* [0009](0009-keycloak-file-secret-exception.md) — the secret-handling exception the drill's IdP forced.
