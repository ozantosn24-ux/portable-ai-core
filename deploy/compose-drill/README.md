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
          │  app  │    │   n8n   │◄──│ n8n-runner │ (external task runners)
          └───┬───┘    └────┬────┘   └────────────┘
              │             │
          drill_app     drill_n8n      ← two databases, two roles, one server
              └──────┬──────┘
                 ┌───┴────┐
                 │postgres│  pgvector, --data-checksums
                 └────────┘
```

Two runner port numbers are easy to conflate, so both are named: the runner **dials out**
to the task broker at `n8n:5679` — measured, that port answers HTTP inside the `n8n`
container, while `5678` is the n8n API — and the runner container itself exposes
`5680/tcp` (`docker compose -p drill ps` shows `drill-n8n-runner-1 … 5680/tcp`). Neither
is published to the host; both are container-network only.

## Run it

```bash
cd deploy/compose-drill
chmod +x scripts/*.sh           # the tree ships them WITHOUT the execute bit (a clone shows
                                #   644 or 666 depending on umask); a fresh clone needs this

scripts/gen_secrets.sh          # 4 secret files, once (no .env - see the script)
scripts/up.sh                   # build + up, times it to ALL-healthy
                                #   (this is the moment to run the two port-claim
                                #    verification commands under "Requirements" below)
scripts/seed.sh                 # 1000 rows + marker, workflow, credential
                                #   (restarts n8n mid-run to make the webhook answer 200)
scripts/verify_tls.sh           # internal-CA TLS, with a negative control
                                #   (writes ca.crt into this directory)
scripts/backup.sh               # dumps + workflow export + key + manifest.json
scripts/restore_drill.sh        # fresh volume, restore, verify, tear down

# Optional `idp` profile (Keycloak 26.7.3 + OIDC-enabled app; see S2-DRILL-2026-09-06.md).
# It is an OVERRIDE, not a second stack: it adds Keycloak and replaces `app` with an
# OIDC-enabled build against the SAME Postgres server, in a third database. It can be
# added to a base stack that is ALREADY RUNNING - idp_up.sh creates Keycloak's role and
# database idempotently first, then reads them back (see the initdb note below).
scripts/idp_up.sh               # 3 more secret files, /etc/hosts entries, --profile idp up
scripts/idp_check.sh alice      # ONE user per run (default: alice); run all three to see
scripts/idp_check.sh bob        #   the cross-user 403s and the ledger rows from each side
scripts/idp_check.sh mia        #   (mia: summaries 200 on both mailboxes, draft 403)

# ---- clean up ----
docker compose -p drill --profile idp down -v   # ONE command tears down base + profile:
                                                #   `down` is not scoped by profile, so a second
                                                #   plain `down -v` only prints "No resource found"
rm -f secrets/*.txt ca.crt                      # the *.example files stay
rm -rf backups key-backups                      # backup.sh output (dumps + the key copy)

# idp_up.sh appended two loopback entries to /etc/hosts and `down -v` does NOT remove
# them. Rewrite the file rather than editing in place: where /etc/hosts is a bind mount
# (any container, a Codespace included) `sed -i` fails with "Device or resource busy",
# because it renames a temporary file over the target.
grep -v 'drill\.internal' /etc/hosts | sudo tee /etc/hosts.new > /dev/null
sudo cp /etc/hosts.new /etc/hosts && sudo rm -f /etc/hosts.new

# Optional: the two locally built images survive the teardown (measured: 336 MB and
# 361 MB reported by `docker images`, sharing most of their layers).
docker rmi drill-app:local drill-app-auth:local
```

Requirements: Docker with Compose v2, `openssl`, `curl`, ~1 GB of RAM for the stack and
about 1 GB of disk for images. Host ports used by the **base** stack: `127.0.0.1:18080` and
`127.0.0.1:18443` only, and Postgres is not reachable from the host at all. That claim is
checkable, so check it rather than trusting it:

```bash
docker compose -p drill ps --format 'table {{.Name}}\t{{.Ports}}'
ss -ltnp | grep -E '18080|18443|5432'
```

Measured 2026-09-07: the only `127.0.0.1:` entries in that table belong to `drill-caddy-1`
(`127.0.0.1:18080->80/tcp`, `127.0.0.1:18443->443/tcp`); `postgres` shows a bare
`5432/tcp`, and `ss` lists 18080 and 18443 with **no line for 5432** — an exposed port is
not a published one.

The optional **`idp` profile publishes two more** — `127.0.0.1:8080` (Keycloak) and
`127.0.0.1:8000` (the API directly, bypassing Caddy) — because an OIDC issuer must resolve
to the *same* URL string from the host and from inside the network. See the header of
`compose.idp.yaml`.

## What each piece is for

| Piece | Why it is in the drill |
|---|---|
| `compose.yaml` | Every image pinned to an exact tag, `depends_on: service_healthy` everywhere, `restart: unless-stopped`, read-only root filesystems and dropped capabilities where the image tolerates them. |
| `Dockerfile.app` | Builds the repository API from `requirements.lock` with `--require-hashes`, runs as uid 10001, and never sees a database password in its environment. |
| `app-entrypoint.sh` | Turns a mounted secret into a 0600 libpq passfile on tmpfs, because `PgVectorStore` refuses a password embedded in the connection URL. |
| `Caddyfile` | `tls internal` for two hostnames; the drill trusts Caddy's own CA explicitly and proves the negative case too. |
| `initdb/` | Creates n8n's (and, for the `idp` profile, Keycloak's) own role and database on first boot and revokes the default `PUBLIC` connect grant, so neither application can read the other's database. ⚠️ Runs **only on an empty `PGDATA`** — see the note below. |
| `scripts/backup.sh` | Dumps, workflow export, and the encryption key **to a separate directory**, with a `manifest.json` carrying sha256 + byte size of every artifact. |
| `scripts/restore_drill.sh` | A second Compose project on an empty volume: restore, then check row count, content digest, table ownership, workflow presence, readiness, and credential decryption. |

⚠️ **Why `idp_up.sh` creates the Keycloak role itself.** `initdb/*` runs once, against an
empty `PGDATA`, so the order in "Run it" — base stack first, `idp` profile afterwards —
would otherwise leave the volume without the `drill_keycloak` role, the extended
healthcheck in `compose.idp.yaml` would (correctly) refuse to go green, and the whole
chain would die with `dependency failed to start: container drill-postgres-1 is unhealthy`.

`scripts/idp_up.sh` therefore creates the role and database idempotently before starting the
profile, and reads them back from the server. The SQL is not duplicated: the script parses
the statements out of `initdb/20-drill-keycloak-db.sh` and refuses to run if they no longer
match the idempotent form it knows how to reproduce.

**A second, worse defect sat underneath that one; it is fixed, and the history is kept
because the symptom was invisible.** `initdb/20-drill-keycloak-db.sh` is mounted into the
**base** stack too, where the `keycloak_db_password` secret is not — and until 2026-09-07 it
aborted there:

```
cat: /run/secrets/keycloak_db_password: No such file or directory
PostgreSQL Database directory appears to contain a database; Skipping initialization
```

Initialisation aborted, the container died, `restart: unless-stopped` brought it back, the
second boot skipped init entirely — and the stack still reported **all six services
healthy**, because the base healthcheck only asserts the `drill_n8n` role that the earlier
`10-` script had already created. It was also a race: of four fresh bring-ups three
self-healed and one failed outright with `dependency failed to start`. The init script now
**skips and says so** — `initdb: keycloak secret not mounted; idp role/db will be ensured by
scripts/idp_up.sh` — instead of aborting. Verified over three consecutive fresh bring-ups:
`RestartCount=0` every time, exactly one PID-1 "ready to accept connections" line every
time, `drill_n8n` present, all healthy in 40.1 / 39.9 / 35.1 s.

⚠️ **`idp_check.sh` is the only thing that proves the profile actually works.** Adding the
profile replaces the `postgres` container, and a Keycloak that was already running then
holds dead connection handles (`PSQLException: This connection has been closed`) — every
container reports healthy, `idp_up.sh` exits 0, and every login still fails. Keycloak's
readiness probe answers on its management port and never touches the database, so it cannot
see this. `idp_up.sh` now detects the replacement and restarts Keycloak itself (measured:
18.0 s, after which `idp_check.sh` passes with no manual step); if you ever reach that state
by another route, `docker compose -p drill -f compose.yaml -f compose.idp.yaml --profile idp
restart keycloak` is the manual equivalent. Measured 2026-09-07; see
`S2-DRILL-2026-09-06.md` §11(f) and TD-077.

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
* **No capacity model — but no longer no load either.** `scripts/load.sh` +
  `scripts/load_client.py` run a bounded load and soak measurement from a throwaway
  container on the drill network, and one recorded run is in
  [`LOAD-2026-09-07.md`](LOAD-2026-09-07.md): `/health` at concurrency 10/50/100, the n8n
  production webhook at 5/20/50, and a 10-minute mixed soak — 166 026 requests, 0 errors,
  `RestartCount=0` throughout. What it found: n8n saturates at ~14 req/s on 2 vCPU and
  absorbs concurrency purely as latency (no 429s, no shedding, no dropped executions), at
  ~2.83 KB of database per execution. What it still does not prove: `/query` was never
  called, TLS handshake cost was excluded by keep-alive, the external runner was never
  exercised (a NoOp workflow dispatches no task), no level was pushed until it failed, and
  there is no failure injection. **Codespace ≠ VPS; sayılar mertebe, kapasite modeli
  değil.** `load.sh` writes raw output to `load-out/`; that directory is runtime output, is
  **not** yet covered by `.gitignore`, and should be deleted with the rest of the teardown.
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
