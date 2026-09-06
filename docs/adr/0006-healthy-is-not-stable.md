# 0006 — "Healthy" is not "stable": health, readiness and restore are asserted, not reported

## Status

Accepted — 2026-09-06.

## Context

`deploy/compose-drill` orders its five services with `depends_on: service_healthy` rather
than with `sleep`, so a healthcheck is not decoration — it is the gate the whole bring-up
runs on. The drill run recorded in `DRILL-2026-09-06.md` found four ways the stack broke
before it worked, and three of them share one shape: **a component reported a green status
that was true about the thing it measured and false about the thing that mattered.**

* `pg_isready` reported a healthy Postgres whose init script had never run (§6.2).
* n8n answered `/healthz/readiness` with 200 before it finished generating static assets, so
  a container reported `healthy restarts=0` at t+60 s and crashed afterwards (§6.3).
* `pg_restore --no-owner` exited 0 and left n8n unable to start (§6.4).

The n8n workflow case is the same family: `publish:workflow` succeeded, every healthcheck was
green, and the production webhook still answered 404 (§3).

## Decision

1. **A healthcheck asserts the object the init step was supposed to create.** The Postgres
   healthcheck is `pg_isready` **and** a `psql` query asserting `pg_roles` contains
   `drill_n8n`, "so a half-initialised database can never report healthy" (§6.2;
   `compose.yaml` healthcheck comment).
2. **Readiness is re-checked after a stabilisation window.** §6.3: "Health at one instant is
   not stability; the run now re-checks `RestartCount` 90 s after all-healthy." §2 records the
   result of that check for this run: all five `restarts=0`.
3. **Readiness of a workflow means the production webhook answers 200, not that the port is
   open** (§3). The measured sequence: `import:workflow` + `publish:workflow` in 10.2 s;
   webhook still 404 after 19.6 s; an n8n restart required; first 200 at 32.1 s. The CLI says
   so itself — `Note: Changes will not take effect if n8n is running.`
4. **`pg_restore` runs without `--no-owner --no-privileges`, and ownership is verified.**
   §6.4: the first attempt used those flags, `pg_restore` exited 0, all 129 n8n tables came
   back owned by the superuser `drill_app`, and n8n crash-looped on its migrations. Dropping
   the flags works because the init script re-creates the roles first, and
   `scripts/restore_drill.sh` now prints table ownership as part of verification.
5. **A restore is verified by decrypting a credential with the backed-up key.** §5: "A fresh
   n8n starts happily against a restored database with the **wrong** key and only fails later,
   when something tries to use a credential — so 'n8n started' proves nothing about the key."
   The restore's phase D therefore checks row count, content digest, marker row, table
   ownership, workflow presence, service readiness *and* `credential_decrypt=OK`.
6. **Green results get a negative control.** §4: the two TLS requests verify with the exported
   internal CA (`ssl_verify_result` 0), and the same requests without `--cacert` fail with curl
   exit code 60 — "without that control a green result would only prove that verification was
   switched off somewhere."
7. **Backups are checked on the way out too.** The dumps get a `PGDMP` magic-byte check at
   backup time, "so a stream mangled between the container and the host is caught at backup
   time, not at restore time", and `manifest.json` carries sha256 + byte size per artifact
   plus the checks the restore has to reproduce (§5).

## Consequences

**Positive**

* `depends_on: service_healthy` becomes trustworthy: healthy now implies the init side effect
  exists, not merely that a port answers.
* The failure that hid behind a false positive in §6.3 is now caught by a second observation
  at a different time — a different *time and layer*, not a second reading of the same instant.
* The backup story is falsifiable end to end: 1001 rows, matching digest, marker row, 129
  tables owned by the n8n role, restored readiness `{"status":"ok"}`, and a decrypted
  credential (§5).

**Negative / not solved**

* **The +90 s re-check is recorded, not automated.** `scripts/up.sh` times the stack to
  all-healthy and exits; no script in `deploy/compose-drill/scripts/` reads `RestartCount`.
  The stabilisation check is described in `DRILL-2026-09-06.md` §2 and §6.3 as a step of that
  run and as a rule ("check RestartCount too", `compose.yaml` n8n comment) — an operator
  running `up.sh` alone does not get it.
* **90 s is a window, not a proof.** Nothing establishes that a crash cannot arrive at
  +91 s; the drill only shows that the previously observed crash arrived inside it.
* **`cap_drop: [ALL]` was refused by Postgres and that is documented, not fixed.** §8 and the
  `compose.yaml` comment: with it the entrypoint crash-loops on
  `chmod: changing permissions of '/var/lib/postgresql/data': Operation not permitted`,
  exit 1, 8 restarts in 25 s. Pre-owned storage and a different entrypoint were out of scope.
* **Restore was never tried on a different host, and corrupt-backup handling stops at the
  `PGDMP` magic-byte check** (§12).
* **No load, concurrency, soak or failure-injection testing.** "One webhook call is a smoke
  test" (§12). No MFA enforcement, no user accounts, no SSO, no key-rotation drill, no ACME.
* **The numbers are order-of-magnitude only.** A shared, virtualised 2 vCPU Codespace with an
  unrelated container holding ~0.99 GiB throughout — the drill says to treat every timing as
  an order of magnitude, not as a capacity model.
* **JSON logs swallow CLI answers.** With `N8N_LOG_FORMAT=json`, the *output* of commands like
  `list:workflow` also arrives as log records, "so a filter that drops info-level lines drops
  the answer" (§10).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| `pg_isready` alone as the Postgres healthcheck | Measured to report healthy on a database whose init script never ran: the first boot died on an unreadable secret, `restart: unless-stopped` restarted it, and the second boot found a non-empty `PGDATA` and skipped initialisation. Evidence: `RestartCount=1` and `database system is ready to accept connections` logged twice (§6.2). |
| `sleep`-based ordering between services | Replaced by `depends_on: service_healthy` — the drill README lists "order enforced by health conditions rather than by `sleep`" as one of the things it proves. |
| Sample health once, shortly after start-up | Exactly what produced the §6.3 false positive: sampled at t+60 s, recorded `health=healthy restarts=0`, and the crash came afterwards; it was caught later only because the webhook began returning 502. |
| Treat "n8n started" as proof the restore worked | A fresh n8n starts happily against a restored database with the wrong encryption key and only fails when a credential is used (§5). |
| `pg_restore --no-owner --no-privileges` | Exits 0, hands 129 tables to the wrong role, and leaves n8n crash-looping on its migrations (§6.4). |
| Treat "the port answers" as workflow readiness | The webhook returned 404 for 19.6 s while every healthcheck was green, and needed an n8n restart before the first 200 at 32.1 s (§3). |
| Accept a green TLS check without a negative control | It would only prove verification was switched off somewhere (§4). |
| `docker cp` files into the n8n container | With `read_only: true` the daemon refuses it outright — `container rootfs is marked read-only` — even for a tmpfs destination; the files are mounted read-only instead (§6.4). |

## Evidence

Source:

* `deploy/compose-drill/compose.yaml` — the Postgres healthcheck and its "MEASURED IN THIS
  DRILL" comment; the n8n healthcheck comment ("/healthz alone only proves the port answers")
  and the "Healthy at t+60s is not stable — check RestartCount too" comment; the long-syntax
  tmpfs with `mode: 01777`; the `cap_drop` note on Postgres; the app comment explaining that
  liveness uses `/health` because `/ready` answers 503 without a trusted identity provider
  (see ADR 0007).
* `deploy/compose-drill/scripts/restore_drill.sh` — the "MEASURED: do NOT pass
  `--no-owner`/`--no-privileges`" comment above the two `pg_restore` calls; the digest, marker,
  table-ownership and `credential_decrypt` checks in phase D.
* `deploy/compose-drill/scripts/up.sh` — times the stack to all-healthy; **contains no
  `RestartCount` check** (the basis for the "recorded, not automated" consequence above).
* `deploy/compose-drill/scripts/backup.sh`, `scripts/verify_tls.sh`,
  `initdb/10-drill-n8n-db.sh`.
* `deploy/compose-drill/DRILL-2026-09-06.md` — §2 (bring-up timings, the +90 s re-check),
  §3 (webhook readiness), §4 (TLS and its negative control), §5 (backup, restore, verification
  output), §6.2, §6.3, §6.4, §8 (hardening accepted per service), §10 (day-1 mapping),
  §12 (not measured).
* `deploy/compose-drill/README.md` — *What this drill proves* / *does NOT prove*.
* Commit `3b3e238` message — "Six failures found and fixed, kept in the record".
  **On the two counts:** the drill document groups its findings into **four** numbered
  subsections (§6.1–§6.4), which is what this ADR's Context refers to; the commit message
  enumerates **six** items, because it lists separately two failures the document folds into
  those subsections — the short-syntax tmpfs `EACCES` and the readiness false positive both
  sit inside §6.3, and the refused `docker cp` into a `read_only` container sits inside §6.4.
  Same failures, two granularities.

Numbers, and where each comes from — every one is copied from `DRILL-2026-09-06.md`, whose
header states that all of its numbers come from one recorded run on a GitHub Codespace
(Ubuntu 24.04, 2 vCPU / 7.76 GiB, Docker 29.7.2, Compose v2.40.3), with an unrelated
container holding ~0.99 GiB throughout:

| Number | Source |
|---|---|
| all-healthy 35.9 s (postgres 9.1 / app 15.1 / n8n 29.8 / runner 30.1 / caddy 35.9) | §2 |
| RestartCount re-checked at +90 s, all five `restarts=0` | §2, §6.3 |
| import+publish 10.2 s; 404 for 19.6 s; first webhook 200 at 32.1 s | §3 |
| TLS `ssl_verify_result` 0; negative control curl exit 60 | §4 |
| backup 8.2 s (`pg_dump` ×2 1.3 s, workflow export 6.3 s) | §5 |
| restore 30.7 s (A 6.7 / B 1.2 / C 17.1 / D 5.7 s) | §5 |
| rows 1001, `digest_match=yes`, 129 tables owned by `drill_n8n`, `credential_decrypt=OK` | §5 |
| `RestartCount=1` and the doubled "ready to accept connections" log | §6.2 |
| Postgres `cap_drop: [ALL]` → exit 1, 8 restarts in 25 s | §8, `compose.yaml` |
| drill total memory ≈ 1,053 MiB; n8n observed at 278 / 330 / 869 / 895 MiB | §9 |

## Related

* ADR 0005 — file secrets; §6.2's uninitialised database is the downstream consequence of the
  permission failure recorded there.
* ADR 0007 — why the drill's app healthcheck uses `/health` and not `/ready`.
* `deploy/compose-drill/DRILL-2026-09-06.md` §12 — the standing list of what remains
  unmeasured.
