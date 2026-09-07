# compose-drill — a single-node deployment drill

A runnable Docker Compose stack that exercises the boring, failure-prone half of shipping
this project: TLS in front, a database with two isolated roles, a workflow runtime with
external task runners, file-based secrets, a backup, and a **restore that is verified
rather than assumed**.

It is a *drill*, not a production deployment. Everything it proves and everything it does
not prove is listed below, and every number in
[`DRILL-2026-09-06.md`](DRILL-2026-09-06.md) came from one recorded run.

```
                  127.0.0.1:18443 (TLS, Caddy internal CA)
                            │
                        ┌───┴────┐
                        │ caddy  │  app.drill.internal  →  app:8000
                        └───┬────┘  n8n.drill.internal  →  n8n:5678
              ┌─────────────┼──────────────┐
              │             │              │
          ┌───┴───┐    ┌────┴────┐   ┌─────┴──────┐
          │  app  │    │   n8n   │───│ n8n-runner │ (external task runners, 5679)
          └───┬───┘    └────┬────┘   └────────────┘
              │             │
          drill_app     drill_n8n      ← two databases, two roles, one server
              └──────┬──────┘
                 ┌───┴────┐
                 │postgres│  pgvector, --data-checksums
                 └────────┘
```

## Run it

```bash
cd deploy/compose-drill

scripts/gen_secrets.sh          # 4 secret files, once (no .env - see the script)
scripts/up.sh                   # build + up, times it to ALL-healthy
scripts/seed.sh                 # 1000 rows + marker, workflow, credential
scripts/verify_tls.sh           # internal-CA TLS, with a negative control
scripts/backup.sh               # dumps + workflow export + key + manifest.json
scripts/restore_drill.sh        # fresh volume, restore, verify, tear down

# optional `idp` profile (Keycloak 26.7.3 + OIDC-enabled app; see S2-DRILL-2026-09-06.md):
scripts/idp_up.sh               # 3 more secret files, /etc/hosts entries, --profile idp up
scripts/idp_check.sh            # end-to-end logins: alice / bob / mia, cross-user 403s, ledger rows

docker compose -p drill down -v                 # clean up (base stack)
docker compose -p drill --profile idp down -v   # clean up when the idp profile was used
```

Requirements: Docker with Compose v2, `openssl`, `curl`, ~1 GB of RAM for the stack and
about 1 GB of disk for images. Host ports used by the **base** stack: `127.0.0.1:18080` and
`127.0.0.1:18443` only, and Postgres is not reachable from the host at all. The optional
**`idp` profile publishes two more** — `127.0.0.1:8080` (Keycloak) and `127.0.0.1:8000`
(the API directly, bypassing Caddy) — because an OIDC issuer must resolve to the *same* URL
string from the host and from inside the network. See the header of `compose.idp.yaml`.

## What each piece is for

| Piece | Why it is in the drill |
|---|---|
| `compose.yaml` | Every image pinned to an exact tag, `depends_on: service_healthy` everywhere, `restart: unless-stopped`, read-only root filesystems and dropped capabilities where the image tolerates them. |
| `Dockerfile.app` | Builds the repository API from `requirements.lock` with `--require-hashes`, runs as uid 10001, and never sees a database password in its environment. |
| `app-entrypoint.sh` | Turns a mounted secret into a 0600 libpq passfile on tmpfs, because `PgVectorStore` refuses a password embedded in the connection URL. |
| `Caddyfile` | `tls internal` for two hostnames; the drill trusts Caddy's own CA explicitly and proves the negative case too. |
| `initdb/` | Creates n8n's own role and database on first boot and revokes the default `PUBLIC` connect grant, so neither application can read the other's database. |
| `scripts/backup.sh` | Dumps, workflow export, and the encryption key **to a separate directory**, with a `manifest.json` carrying sha256 + byte size of every artifact. |
| `scripts/restore_drill.sh` | A second Compose project on an empty volume: restore, then check row count, content digest, table ownership, workflow presence, readiness, and credential decryption. |

## What this drill proves

* A pinned-image Compose stack comes up healthy from nothing, with the order enforced by
  health conditions rather than by `sleep`.
* File-based secrets (`*_FILE`) are actually honoured **by the services that support
  them** — verified by observation: no plaintext value in the container environment or in
  `docker inspect`, and the key n8n persisted hashes identically to the key file it was
  told to read.
  ⚠️ **Keycloak is the measured exception.** 26.7.3 ignores both
  `KC_BOOTSTRAP_ADMIN_PASSWORD_FILE` and `KC_DB_PASSWORD_FILE`, so the `idp` profile uses
  an entrypoint shim that exports them into the process environment. For that container
  the claim narrows to: nothing in `docker inspect` and nothing via `docker exec … env`,
  but the values **are** readable in `/proc/1/environ`. Measurements and the exact errors
  are in [`S2-DRILL-2026-09-06.md`](S2-DRILL-2026-09-06.md) §7–§8, and the trade-off is
  recorded in [ADR 0009](../../docs/adr/0009-keycloak-file-secret-exception.md).
* External n8n task runners register against the broker over an authenticated channel.
* TLS terminates at the proxy with a certificate that verifies against a specific CA —
  and the same request fails without that CA.
* A backup can be restored into an empty database and the result is *checked*: row count,
  content digest, marker row, table ownership, workflow presence, service readiness, and
  decryption of a credential using the separately backed-up encryption key.

## What this drill does NOT prove

* **Single node.** One machine, one Postgres, no replication, no failover, no quorum.
  Nothing here says anything about surviving the loss of the host.
* **A Codespace is not a VPS.** The measurements come from a 2 vCPU / 7 GB GitHub
  Codespace with an overlay filesystem, sharing the machine with an unrelated container.
  Treat the timings as an order of magnitude, not as a capacity model.
* **No ACME, no public domain.** `tls internal` proves the proxy and the chain; it proves
  nothing about public certificate issuance, renewal, DNS, or HSTS.
* **No sustained load.** One webhook call is a smoke test. There is no concurrency,
  soak, or failure-injection testing here, and no measurement of behaviour under memory
  pressure.
* **No secrets vault.** Secrets are files on disk protected by a directory mode. That is
  better than values in `compose.yaml` and much worse than a managed secret store with
  rotation and audit. There is no rotation drill here.
* **n8n community edition limits apply.** Execution data is stored unencrypted in the
  database (only credentials are encrypted), and there is no SSO, no log streaming, no
  multi-main high availability, and no external secret-store integration. Retention is
  bounded by pruning settings, not by anything stronger.
* **The data is synthetic.** Generated rows, a fake credential whose value is literally
  `drill-fake-not-a-secret`, and a workflow that does nothing. No real secret, customer,
  or production dataset touched this stack at any point.

## Secrets

`scripts/gen_secrets.sh` writes four files into `secrets/` for the base stack, and nothing
else. `scripts/idp_up.sh` adds three more when the `idp` profile is used
(`keycloak_db_password`, `keycloak_admin_password`, `session_secret`), so **seven**
`*.example` files ship in total. All real secret material is git-ignored; only the
`*.example` files are committed. There is deliberately no `.env` with a plaintext token in
it — see the runner note in `compose.yaml`.

The permission model is a measured compromise, not a preference: outside swarm mode
Compose mounts a file secret with the **host** file's owner and mode and warns that
`uid`, `gid` and `mode` in the long syntax "are not supported, they will be ignored".
The containers do not share a uid — postgres runs as 999, n8n and the runner as 1000,
the app image as 10001 — so a 0600 file owned by the deploy user is unreadable to most of
them. The directory is therefore `0700` and the files inside are `0644`: other host users
are blocked by the directory, and every container user can still read the file it needs.
Details, including the failure this was diagnosed from, are in `DRILL-2026-09-06.md`.

The n8n encryption key is generated **before** n8n first starts. If n8n generates its own
key on first boot, the only copy lives inside the container volume, and the first time
that volume is lost every stored credential becomes unreadable.
