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

scripts/gen_secrets.sh          # 4 secrets + .env, once
scripts/up.sh                   # build + up, times it to ALL-healthy
scripts/seed.sh                 # 1000 rows + marker, workflow, credential
scripts/verify_tls.sh           # internal-CA TLS, with a negative control
scripts/backup.sh               # dumps + workflow export + key + manifest.json
scripts/restore_drill.sh        # fresh volume, restore, verify, tear down

docker compose -p drill down -v # clean up
```

Requirements: Docker with Compose v2, `openssl`, `curl`, ~1 GB of RAM for the stack and
about 1 GB of disk for images. Host ports used: `127.0.0.1:18080` and `127.0.0.1:18443`
only — nothing else is published, and Postgres is not reachable from the host at all.

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
* File-based secrets (`*_FILE`) are actually honoured — verified by observation: no
  plaintext value in the container environment or in `docker inspect`, and the key n8n
  persisted hashes identically to the key file it was told to read.
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

`scripts/gen_secrets.sh` writes four files into `secrets/`, and nothing else. All of it is
git-ignored; only the `*.example` files are committed. There is deliberately no `.env`
with a plaintext token in it — see the runner note in `compose.yaml`.

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
