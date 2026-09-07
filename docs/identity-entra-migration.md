# Swapping the IdP: Keycloak → Microsoft Entra ID

What actually changes in `wozto_ai_reference.identity` when the OpenID Provider is
Microsoft Entra ID instead of the Keycloak realm the drill runs against.

**How to read this.** Every Entra fact carries the Microsoft Learn page it was read from
and the date it was opened. Every Keycloak fact is either **measured** against the running
`quay.io/keycloak/keycloak:26.7.3` in `deploy/compose-drill` (`S2-DRILL-2026-09-06.md`) or
marked `[UNVERIFIED]`. Nothing here was written from memory, and no tenant id, GUID, or
example value was invented — the placeholders below (`{tenant}`, `{realm}`) are the
vendors' own.

⛔ **No Entra tenant was contacted.** This document is a reading of the documentation plus
a measurement of Keycloak. The migration itself is untested; treat the table as the list
of things to check, not as a completed port.

---

## 1. The short version

The relying party in `identity/oidc.py` is configuration-driven precisely so this swap is
a config change. Six `OidcConfig` fields carry the whole difference:

| `OidcConfig` field | Keycloak (measured) | Entra ID (documented) |
|---|---|---|
| `issuer` | `http://idp.drill.internal:8080/realms/drill` | `https://login.microsoftonline.com/{tenant}/v2.0` |
| `discovery_url` | derived: `{issuer}/.well-known/openid-configuration` | derived the same way — the shape matches |
| `client_id` | `drill-app` | the Application (client) ID GUID |
| `client_secret` + `token_endpoint_auth_method` | `None` + `"none"` (public client) | see §4 — likely a confidential client |
| `roles_claim_path` | `("realm_access", "roles")` | `("roles",)` or `("groups",)` — §5 |
| `tenant_claim_path` | unused; static `tenant_id` | `("tid",)` — §7, and validating it is **mandatory** |

Everything else — PKCE S256, `state`, `nonce`, JWKS caching with refetch-on-unknown-`kid`,
bounded clock skew — is the same protocol on both sides and needs no code change.

---

## 2. Issuer and discovery

* Entra's tenant-scoped authority is `https://login.microsoftonline.com/{tenant}/v2.0`;
  "All endpoints (except UserInfo) are served under the tenant-scoped authority". A v2.0
  issuer "ends in `/v2.0`". [[v2-protocols-oidc](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc), [id-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference) — opened 2026-09-06]
* The v1.0 issuer is a **different host**: `https://sts.windows.net/<tenant GUID>/`, with no
  `/v2.0` suffix. [[signing-key-rollover](https://learn.microsoft.com/en-us/entra/identity-platform/signing-key-rollover) — opened 2026-09-06]
* Discovery: `https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration`,
  where `{tenant}` is `common`, `organizations`, `consumers`, a tenant GUID, or a verified
  domain. [[v2-protocols-oidc](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc) — opened 2026-09-06]
* JWKS lives on a **different path family** — `/{tenant}/discovery/v2.0/keys`, *not* under
  `/oauth2/v2.0/`. Endpoints: `/{tenant}/oauth2/v2.0/authorize`, `/{tenant}/oauth2/v2.0/token`,
  `/{tenant}/oauth2/v2.0/logout`. UserInfo is on another host entirely
  (`https://graph.microsoft.com/oidc/userinfo`). [[v2-protocols-oidc](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc) — opened 2026-09-06]
* Keycloak, **measured** in the drill: issuer `<base>/realms/<realm>`, discovery
  `<issuer>/.well-known/openid-configuration`, and every endpoint under
  `<issuer>/protocol/openid-connect/{auth,token,certs,userinfo}` — see the measured
  discovery document in `S2-DRILL-2026-09-06.md §3`.

⚠️ **`common` / `organizations` is never the issuer.** "This endpoint isn't a tenant or an
issuer itself"; the `iss` returned carries the *user's* tenant GUID. A multi-tenant RP
"must validate that the `issuer` property in the published metadata matches the `iss` claim
in the token, in addition to the usual check that the `iss` claim in the token contains the
tenant ID (`tid`) claim."
[[howto-convert-app-to-be-multi-tenant](https://learn.microsoft.com/en-us/entra/identity-platform/howto-convert-app-to-be-multi-tenant) — opened 2026-09-06]

🔴 **This is the one place the current code is single-tenant only.** `_validate_claims`
compares `iss` byte-for-byte against the configured `issuer` (`secrets.compare_digest`).
That is correct and strict for a single tenant and for Keycloak. For a multi-tenant Entra
app it is wrong in both directions: the configured `common` authority never equals the
`iss` you receive. Supporting multi-tenant means an issuer *template* plus a `tid`
allowlist, and it is deliberately not implemented here.

## 3. Audience

* ID token: "the audience is your app's Application ID... The token should be rejected if
  it fails to match your app's Application ID." That is exactly what `_validate_claims`
  does today. [[id-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference) — opened 2026-09-06]
* Access token v2.0: `aud` is "always the client ID of the API"; v1.0: an App ID URI such as
  `api://{ApplicationID}`. [[access-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference), [claims-validation](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation) — opened 2026-09-06]
* ⛔ **Do not validate tokens that are not yours.** "Access tokens are only validated in the
  web APIs for which they were acquired... The client shouldn't validate access tokens", and
  "Don't attempt to validate or read tokens for any API you don't own... Tokens for Microsoft
  services can use a special format that will not validate as a JWT". This code never parses
  the access token — it forwards it as a bearer credential to UserInfo and nothing else.
  Keep it that way. [[claims-validation](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation), [v2-oauth2-auth-code-flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow) — opened 2026-09-06]
* "Do not use ID tokens for authorization purposes." [[id-tokens](https://learn.microsoft.com/en-us/entra/identity-platform/id-tokens) — opened 2026-09-06] In this design the ID token
  establishes *identity only*; every authorization decision comes from the grant table in
  `identity/authz.py`. That split already matches the guidance.

## 4. Client credentials and PKCE

* The discovery document advertises `"token_endpoint_auth_methods_supported":
  ["client_secret_post", "private_key_jwt"]`. [[v2-protocols-oidc](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc) — opened 2026-09-06]
  ⇒ `token_endpoint_auth_method="client_secret_post"` is supported by `OidcConfig` today.
  **`client_secret_basic` is not in that list** — the default HTTP-Basic form this code also
  supports would need checking before use.
* Certificate credential = `private_key_jwt`: `alg` "Should be **PS256**", header `x5t#S256`,
  assertion `aud` = `https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token`,
  `iss` = `sub` = client id, `client_assertion_type` =
  `urn:ietf:params:oauth:client-assertion-type:jwt-bearer`. [[certificate-credentials](https://learn.microsoft.com/en-us/entra/identity-platform/certificate-credentials) — opened 2026-09-06]
  🔴 **Not implemented.** `_exchange_code` knows only `none` / `client_secret_post` /
  `client_secret_basic`. Certificate credentials are a code change, not a config change.
* PKCE: `code_challenge` "is now recommended for all application types, both public and
  confidential clients, and **required by the Microsoft identity platform for single page
  apps**"; `code_challenge_method` "*SHOULD* be `S256`... The Microsoft identity platform
  supports both `plain` and `S256`." [[v2-oauth2-auth-code-flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow) — opened 2026-09-06]
  This code only ever sends `S256` and has no `plain` path, which is stricter than required.
* Platform type is a property of the **registration**, not the request: the manifest holds
  three separate buckets (`web.redirectUris`, `spa.redirectUris`, `publicClient.redirectUris`).
  A server-side Python web app maps to the **Web** platform. [[reference-microsoft-graph-app-manifest](https://learn.microsoft.com/en-us/entra/identity-platform/reference-microsoft-graph-app-manifest), [reply-url](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url) — opened 2026-09-06]
* Entra enforces the platform structurally: a `spa` redirect URI "returns an error if you
  attempt to use [it] without an `Origin` header", and client credentials are blocked "in the
  presence of an `Origin` header". [[v2-oauth2-auth-code-flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow) — opened 2026-09-06]

**[UNVERIFIED]** Whether a **`web`**-platform redirect URI can be used by a *secretless*
client (PKCE-only, no `client_secret`/`client_assertion`). None of the pages opened answers
this directly; the closest statements are "required for confidential web apps" and the
`isFallbackPublicClient` fallback semantics, neither of which settles it. **Plan for a
confidential client with a secret or certificate** until this is checked against a real
tenant — that is the safe direction to be wrong in.

## 5. Roles and groups — the biggest practical difference

Keycloak nests realm roles at `realm_access.roles`. Entra has no such nesting, and it has
three different claims for three different things:

| Claim | What it is | Turned on by |
|---|---|---|
| `roles` | "The set of roles that were assigned to the user" (app roles) | app-role assignment |
| `groups` | "JSON array of GUIDs" of group object ids | `groupMembershipClaims` |
| `wids` | tenant-wide directory roles (RoleTemplateID GUIDs) | `groupMembershipClaims` = `All`/`DirectoryRole` |

[[id-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference), [access-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference) — opened 2026-09-06]

⇒ `roles_claim_path=("roles",)` or `("groups",)`. `claim_path_value` walks a single-segment
path fine, so this really is one config line.

`groupMembershipClaims` valid values: `None`, `SecurityGroup`, `ApplicationGroup`,
`DirectoryRole`, `All`. The emitted *value* is switched with `optionalClaims`
`additionalProperties`: `sam_account_name`, `dns_domain_and_sam_account_name`,
`netbios_domain_and_sam_account_name`, `emit_as_roles`, `cloud_displayname`; by default
group **object IDs** are emitted. [[reference-microsoft-graph-app-manifest](https://learn.microsoft.com/en-us/entra/identity-platform/reference-microsoft-graph-app-manifest), [optional-claims](https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims) — opened 2026-09-06]

⚠️ **Footgun:** adding `emit_as_roles` moves group values into the role claim and then
"any application roles configured that the user... is assigned aren't in the role claim" —
app roles are **replaced**, not merged. [[optional-claims](https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims) — opened 2026-09-06]

🔴 **Group overage — the claim can simply be absent.** "If a user is a member of more groups
than the overage limit (**150 for SAML tokens, 200 for JWT tokens**), then Microsoft Entra ID
doesn't emit the groups claim in the token." Instead the token carries
`"_claim_names": { "groups": "src1" }` + `"_claim_sources": { "src1": { "endpoint": ... } }`,
or `hasgroups: true`. [[access-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference) — opened 2026-09-06]

**What this code does today:** `_as_role_set(None)` returns an empty `frozenset` — no roles,
not an error. Combined with the grant table that is fail-closed (a principal with no roles
still needs a grant, and a grant is what actually authorizes), so an overage produces
*denial*, never accidental access. But it produces a **silent** denial for exactly the
users who are in the most groups. Either set `groupMembershipClaims` to `ApplicationGroup`
("recommended for large organizations due to the group number limit in token"), or detect
`_claim_names`/`hasgroups` explicitly and fail loudly. The `fetch_userinfo` merge in this
code is *not* a fix: Entra's UserInfo is Microsoft Graph's `/oidc/userinfo` and does not
resolve group overage.

⚠️ "Never use claims like `email`, `preferred_username` or `unique_name` to store or
determine whether the user... should have access to data." [[claims-validation](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation) — opened 2026-09-06]
The drill's grant table is keyed by `sub`, which satisfies this — **do not** be tempted to
key it by username because the Keycloak UUIDs are unreadable.

## 6. Redirect URI

* `https` only, "with exceptions for some localhost redirect URIs"; case-sensitive; exact
  match; no `! $ ' ( ) , ;`; no Internationalized Domain Names; **256 URIs** per app (100 for
  MSA-enabled apps), **256 characters** each. [[reply-url](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url) — opened 2026-09-06]
* localhost: `http` is accepted, and **the port is ignored when matching** — so
  `http://localhost:1234/MyApp` and `http://localhost:5000/MyApp` are the same URI. "Do not
  register multiple localhost redirect URIs where only the port differs. The login server
  picks one arbitrarily." `[::1]` is unsupported; `127.0.0.1` + `http` can only be added by
  editing the manifest. [[reply-url](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url) — opened 2026-09-06]
* Wildcards: allowed only for work/school-only apps, strip query and fragment, and are
  "strongly" discouraged. [[reply-url](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url) — opened 2026-09-06]
* If `redirect_uri` is omitted entirely, "the endpoint picks one registered `redirect_uri` at
  random". [[v2-protocols-oidc](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc) — opened 2026-09-06] This code always sends it, so that hazard does not apply.

⇒ The drill's `http://app.drill.internal:8000/auth/callback` is **not** registrable in Entra
(plain `http`, non-localhost). Real deployment needs `https`.

## 7. `nonce`, `tid`, `ver` — and what Keycloak does not require

* `nonce` is **Required** on an Entra OIDC sign-in request, and "your app should verify the
  `nonce` value in the ID token is the same value it sent". Already enforced, and there is a
  mutation test proving the check is load-bearing. [[v2-protocols-oidc](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc), [id-tokens](https://learn.microsoft.com/en-us/entra/identity-platform/id-tokens) — opened 2026-09-06]
* 🔴 **`tid` validation is mandatory and is not implemented as a check.** "Always check that
  the `tid` in a token matches the tenant ID used to store data with the application... Never
  allow data in one tenant to be accessed from another tenant." [[claims-validation](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation) — opened 2026-09-06]
  Setting `tenant_claim_path=("tid",)` *maps* `tid` into `Principal.tenant_id`, which is then
  used for tenant isolation downstream — but the identity layer never asserts it is an
  **expected** tenant. For a single-tenant app the `iss` equality check covers it; for
  anything multi-tenant it does not, and an allowlist is required.
* Identity is per-tenant: "If a single user exists in multiple tenants, the user contains a
  different object ID in each tenant — they're considered different accounts." So the grant
  table key must be `(tid, sub)`, not `sub` alone, the moment a second tenant exists. Today
  `MailboxGrant.principal_id` is `Principal.user_id` = `sub` only.
  [[id-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference) — opened 2026-09-06]
* `ver` is `"1.0"` or `"2.0"`; several claims exist in only one version (`preferred_username`
  and `azp` are v2.0-only; `appid`, `unique_name`, `upn` are v1.0-only). The `x5t` header is a
  legacy v1.0 duplicate of `kid`. This code reads `kid` only.
  [[access-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference) — opened 2026-09-06]
* `requestedAccessTokenVersion` (Graph app manifest, under `api`) selects the access-token
  version, `1`/`2`/`null`, defaulting to 1. "The endpoint used, v1.0 or v2.0, is chosen by the
  client and only impacts the version of id_tokens." [[reference-microsoft-graph-app-manifest](https://learn.microsoft.com/en-us/entra/identity-platform/reference-microsoft-graph-app-manifest) — opened 2026-09-06]
  **[UNVERIFIED]** the legacy name `accessTokenAcceptedVersion` — the Azure AD Graph manifest
  page was not opened, so it is not claimed that the two names are the same setting.
* "Applications should not take hard dependency on claims being present or in specific order."
  `claim_path_value` returning `None` on a missing path already respects this.
  [[access-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference) — opened 2026-09-06]
* Authorization codes "expire after about 1 minute" — shorter than most IdPs. The
  single-use pending-login session in `web.py` is well inside that.
  [[v2-oauth2-auth-code-flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow) — opened 2026-09-06]
* **[UNVERIFIED]** "Tenant restrictions" as a named Entra feature — no such page was opened.
  What *is* verified is the app-side equivalent: "Your app should use the GUID portion of the
  claim to restrict the set of tenants that can sign in to the app."

## 8. Signing-key rollover — the design this code already follows

Entra's guidance and `oidc.py`'s JWKS cache agree almost line for line, which is worth
recording because it means the rotation behaviour is not improvised:

| Microsoft guidance [[signing-key-rollover](https://learn.microsoft.com/en-us/entra/identity-platform/signing-key-rollover) — opened 2026-09-06] | This code |
|---|---|
| "There's no set or guaranteed time between these key rolls" | no assumption about cadence anywhere |
| cache keys individually by `kid`; order is not meaningful | `KeySet.get_by_kid`, order never used |
| refresh "once on process startup or when cache is empty" | `_signing_key` loads when `key_set is None` |
| refresh "dynamically if a received token was signed with an unknown key" | one refetch on unknown `kid`, then reject |
| "but no more frequently than 5 mins" | `jwks_min_refetch_interval_s` — **default 300.0 s** |
| "always more than one valid key available" | multi-key JWKS handled; a missing `kid` with >1 key fails closed rather than guessing |

The one gap: Microsoft also recommends a **periodic background refresh** — cache keys with a
24 h TTL and refreshes "every 1 hour", plus automatic background updates at "12 h with a
jitter of plus or minus 1 h". [[signing-key-rollover](https://learn.microsoft.com/en-us/entra/identity-platform/signing-key-rollover) — opened 2026-09-06]
This code has no background refresh — it is purely lazy plus refetch-on-unknown-`kid`. That
is sufficient for correctness (an unknown `kid` always triggers a fetch) but means the first
request after a rollover pays the fetch latency.

## 9. Not covered here

* No Entra tenant was contacted; nothing in §1–§8 was executed.
* No token refresh, no back-channel or front-channel logout against Entra (Entra's logout
  endpoint is `/{tenant}/oauth2/v2.0/logout`; this code only clears its own session).
* No Conditional Access, MFA, or device-compliance behaviour was measured.
* No B2C / External ID: those use different authority shapes entirely and none were read.
* Keycloak comparison facts here are **measured from the drill**, not from Keycloak's docs
  — one Keycloak documentation URL returned HTTP 404 during research, so the running server
  was used as the source of truth instead. That is the stronger evidence anyway.
