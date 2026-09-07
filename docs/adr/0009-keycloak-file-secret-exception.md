# 0009 — Keycloak cannot read file secrets: a measured exception to 0005

## Status

Accepted 2026-09-06. **Amends [0005](0005-file-based-secrets-in-compose.md)** — it does not
supersede it. 0005 keeps its text and its rule; this record narrows the rule's scope to the
services that can actually honour it, and states exactly what is given up for the one that
cannot.

## Context

0005 states the requirement plainly: a secret *"must never appear in an image, an
environment variable, or `docker inspect`"*, and every service in the base drill honours it
through `*_FILE` variables — Postgres, n8n and the task runner were all verified by
observation.

The `idp` profile added Keycloak, and Keycloak breaks that rule. It is not configuration
drift or a missing option; the image simply does not implement it. Measured against
`quay.io/keycloak/keycloak:26.7.3`, the two failures do not even look alike:

```
KC_BOOTSTRAP_ADMIN_PASSWORD_FILE  ->  crash loop, never reaches startup:
    "bootstrap-admin-username available only when bootstrap admin password is set"

KC_DB_PASSWORD_FILE               ->  starts, then cannot reach Postgres:
    org.postgresql.util.PSQLException: The server requested SCRAM-based authentication,
    but no password was provided.
```

⚠️ **`kc.sh show-config` is a false witness, and it nearly produced a wrong ADR.** Asked
about the database password it prints:

```
kc.db-password-file =  ******* (ENV)
```

which reads as "the option was accepted". It was not. `show-config` echoes the key; it does
not prove the option is *used*. The first version of the shim was written believing the DB
half worked and only the admin half was broken — the connection attempt disproved it. *One
hit is not a classification; a tool that answers a nearby question is not evidence.*

## Decision

Keep `*_FILE` everywhere it works. For Keycloak alone, mount an entrypoint shim
(`deploy/compose-drill/idp/keycloak-entrypoint.sh`) that maps `KC_<OPTION>_FILE` →
`KC_<OPTION>`, then `exec`s the real entrypoint. Compose still passes only file paths; the
container turns them into environment variables at start.

The exception is scoped as narrowly as possible:

* it applies to one service in one optional profile;
* it is generic over `KC_*_FILE` rather than hard-coding two names, so a future Keycloak
  option cannot quietly regress to a value in `compose.yaml`;
* it **fails loudly** if a secret file is unreadable (`exit 1` naming the variable and the
  path) rather than starting with an empty password.

## What is actually given up — measured three ways

| Observation point | Secret present? |
|---|---|
| `docker inspect .Config.Env` | **No** — only `KC_BOOTSTRAP_ADMIN_PASSWORD_FILE=` / `KC_DB_PASSWORD_FILE=` paths |
| `docker exec <container> env` | **No** — grep count 0 |
| `/proc/1/environ` | **Yes** — grep count 2 |

⚠️ The middle row corrects a claim written before it was measured. The shim's first header
said `docker exec … env` would expose the values. It does not: `docker exec` starts a new
process from the image/Compose environment and does not inherit what the entrypoint
exported. The real exposure is PID 1's environment.

So 0005's rule survives in two of its three clauses even for Keycloak — nothing in the
image, nothing in `docker inspect` — and is genuinely broken in the third: the value is an
environment variable of the running process, readable by anything inside that container
that can read `/proc/1/environ`.

## Consequences

* An attacker with code execution *inside the Keycloak container* reads both the bootstrap
  admin password and the database password. Before the shim they would have read the same
  values from `/run/secrets/*` anyway, so the marginal loss is smaller than it looks — the
  real regression is that the value now also survives in a process listing context and in
  any crash dump or debugger attached to PID 1.
* Blast radius is bounded by what those credentials reach: the `drill_keycloak` role can
  connect only to its own database (`initdb/20-drill-keycloak-db.sh` revokes `PUBLIC`
  connect), and the bootstrap admin is a drill account in a dev-mode realm.
* The base stack's claim is now qualified rather than global. `deploy/compose-drill/README.md`
  says "honoured by the services that support them" and names the exception.
* A `docker inspect`-only audit of this stack would report "clean" and be wrong for one
  container. Any future check must read `/proc/1/environ` as well.

## Closing path

Ranked by what would actually remove the exposure:

1. **A real secret manager with short-lived credentials** — the value in the process
   environment stops mattering once it expires in minutes.
2. **Keycloak gaining `*_FILE` support** — re-run the two controls in this record after any
   version bump; if the crash loop and the SCRAM failure are gone, delete the shim and this
   exception with it.
3. Not viable as written: passing the value in `compose.yaml`, which is what 0005 exists to
   prevent and is strictly worse than the shim.

## Evidence

* Both exact errors, the `show-config` false witness, and the three-point exposure
  measurement: [`S2-DRILL-2026-09-06.md`](../../deploy/compose-drill/S2-DRILL-2026-09-06.md)
  §7(a) and §8.
* The shim and its header: `deploy/compose-drill/idp/keycloak-entrypoint.sh`.
* Image tag pinned and pull-verified: `quay.io/keycloak/keycloak:26.7.3`. The digest observed
  at pull time was `sha256:ff4257d0d64efbe99ed1ddfaf07765cc3c36dc7518bf8324d41961327f441c54`;
  it is **recorded, not enforced** — `compose.idp.yaml` pins the tag only, not `@sha256:`.

## Related

* [0005](0005-file-based-secrets-in-compose.md) — the rule this amends.
* [0008](0008-oidc-relying-party-and-mailbox-grants.md) — the identity work that needed an IdP.
* [0006](0006-healthy-is-not-stable.md) — the same discipline of asserting rather than reporting.
