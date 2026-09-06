# 0005 — File-based secrets in Compose, and the mode trap that had to be measured

## Status

Accepted — 2026-09-06.

## Context

`deploy/compose-drill` is a runnable single-node stack (Postgres + the repository API +
Caddy + n8n + an external task runner) whose purpose is to exercise "the boring,
failure-prone half of shipping this project" (`deploy/compose-drill/README.md`). Secrets are
one of those halves: five services, three different container users, and four values that
must never appear in an image, an environment variable, or `docker inspect`.

Two facts forced the design, and both had to be measured rather than assumed:

* **Compose file secrets outside swarm mode do not honour `uid`, `gid` or `mode`.** Compose
  v2.40.3 warns about it explicitly; `DRILL-2026-09-06.md` §6.1 records the line as:

  ```
  level=warning msg="secrets `uid`, `gid` and `mode` are not supported, they will be ignored"
  ```
* **Vendor documentation can be wrong.** n8n's docs state the task-runner image cannot use
  file-based configuration and that "variables with a `_FILE` suffix added will not be
  recognized". Measured against `n8nio/runners:2.36.7`, that is not true (§7).

## Decision

1. **Secrets are files, never inline values.** `compose.yaml`'s header says so, and
   `scripts/gen_secrets.sh` is the only writer; the whole `secrets/` directory is git-ignored
   and only `*.example` files are committed.
2. **Permissions are set on the host, because Compose will not set them.** `gen_secrets.sh`
   runs `chmod 700 secrets` and `chmod 644` on each file. The rationale is written in both
   the script and `compose.yaml`'s `secrets:` block: the container users differ — postgres
   999, n8n and the runner 1000, the app image 10001 — so a 0600 file owned by the deploy user
   is unreadable to most of them. Protection therefore comes from the **directory**, and the
   files inside are readable by every container user and by no other host user.
3. **`_FILE` variables everywhere, including the runner.** `POSTGRES_PASSWORD_FILE`,
   `N8N_ENCRYPTION_KEY_FILE`, `DB_POSTGRESDB_PASSWORD_FILE`,
   `N8N_RUNNERS_AUTH_TOKEN_FILE`. There is deliberately no `.env` holding a plaintext token
   (`deploy/compose-drill/README.md`, *Secrets*).
4. **The runner decision rests on measured controls, not on the docs.** The three controls
   appear both in `DRILL-2026-09-06.md` §7 and in the `compose.yaml` comment above the runner
   service: `_FILE` + correct token → the broker logs `Registered runner "launcher-javascript"`
   / `"launcher-python"`; `_FILE` + **wrong** token → never registers, broker silent for 20 s;
   no token variable at all → `Failed to load config: AuthToken: missing required value:
   N8N_RUNNERS_AUTH_TOKEN`. The fourth row — plain `N8N_RUNNERS_AUTH_TOKEN` with the correct
   token → registers — is the positive control, and it appears **only in §7's table**, not in
   the `compose.yaml` comment. Rows 1 and 2 differ only in the file's *contents*, and row 3
   proves the variable is mandatory, so the file is read.
5. **The app never receives a password in its environment.** `PgVectorStore` refuses a
   password embedded in the connection URL, so `app-entrypoint.sh` turns the mounted secret
   into a `0600` libpq passfile on tmpfs.
6. **The encryption key is generated before n8n first starts.** If n8n generates its own key
   on first boot, the only copy lives inside the container volume, "and the first time that
   volume is lost every stored credential becomes unreadable"
   (`deploy/compose-drill/README.md`).
7. **Every claim about secrets is stated as an observation with its evidence** (§7 table):
   `POSTGRES_PASSWORD` in container env — 0 occurrences; `N8N_ENCRYPTION_KEY` in env and in
   `docker inspect .Config.Env` — 0; the sha256 of the key n8n persisted to
   `/home/node/.n8n/config` equals the sha256 of the secret file (`34cd451e…` on both sides);
   `PGPASSWORD` in the app env — 0, with the passfile `600 appuser:appuser` on a
   `tmpfs (rw,nosuid,nodev,noexec)` mount; `has_database_privilege('drill_n8n','drill_app','CONNECT')`
   → `f`; `SHOW data_checksums` → `on`.

The same shape is enforced for the repository's own root `compose.yaml` by
`tests/test_compose_secret_contract.py`, which asserts `POSTGRES_PASSWORD:` is absent,
`POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password` is present, `WOZTO_RAG_DB_PASSWORD_FILE`
is used, and that the README's connection examples use `passfile=` rather than an inline
password.

## Consequences

**Positive**

* Three specific variable names were measured at **0 occurrences** rather than assumed absent
  (§7): `POSTGRES_PASSWORD` in the container environment, `N8N_ENCRYPTION_KEY` in the
  container environment *and* in `docker inspect .Config.Env`, and `PGPASSWORD` in the app
  environment.
  **Scope of that claim:** §7 counts those three names and no others. `runner_auth_token` and
  `n8n_db_password` have no occurrence count; their §7 rows rest on behavioural evidence
  instead (the runner registers or fails to register depending on the file's contents; n8n
  connects and migrates with no password variable set). No image-layer scan was performed
  anywhere in the drill — "no password in the image" is an assertion in the header comment of
  `app-entrypoint.sh`, not a measurement.
* A wrong or missing runner token fails loudly at startup rather than silently degrading
  (row 3 of the control table).
* The key backup is meaningful, because the key exists before the first credential is written
  (ADR 0006 covers the restore that proves it decrypts).

**Negative / not solved**

* **0644 is a real weakening of the files themselves.** Anything running as any container
  user — or any host process that can traverse the directory — can read them. The drill
  README states this as "a measured compromise, not a preference".
* **No secrets vault, no rotation.** `deploy/compose-drill/README.md`, *What this drill does
  NOT prove*: "Secrets are files on disk protected by a directory mode. That is better than
  values in `compose.yaml` and much worse than a managed secret store with rotation and
  audit. There is no rotation drill here." §12 adds that
  `N8N_ENV_FEAT_ENCRYPTION_KEY_ROTATION` was never exercised.
* **Every finding is version-scoped.** The mode behaviour is Compose v2.40.3; the `_FILE`
  behaviour is `n8nio/runners:2.36.7`. Both the compose comment and §7 instruct re-running
  the three controls after a version bump rather than trusting the recorded line.
* **n8n community-edition limits still apply.** Execution data is stored unencrypted in the
  database; only credentials are encrypted (drill README).
* **This is a drill, not a production deployment**: single node, no replication, no MFA
  enforcement, no SSO, and the measurements come from a shared 2 vCPU / 7 GB Codespace.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Inline secret values / `environment:` entries in `compose.yaml` | The whole point of the drill's secret handling; values would appear in `docker inspect` and in the file itself. §7 measures the absence instead of assuming it. |
| Long-syntax `uid` / `gid` / `mode` on the Compose secret | Measured: outside swarm mode Compose v2.40.3 ignores them and warns that it does. The probe (a container running as uid 10001 against a 0600 host file) saw `-rw------- 1 1000 1000` and `Permission denied`; the drill's first run died with `cat: /run/secrets/n8n_db_password: Permission denied`. |
| Keep files at 0600 and align container users | The three images run as 999, 1000 and 10001; aligning them would mean rebuilding vendor images. Not attempted — **rationale for not pursuing this is not recorded in the repo at the time of this ADR; inferred from `deploy/compose-drill/README.md`'s framing of the 0700/0644 split as the chosen compromise.** |
| Trust n8n's documentation that the runner cannot read `_FILE`, and use a plaintext `.env` | Rejected on measurement: three controls plus a positive control show the file *is* read (§7), so the plaintext copy was avoidable. |
| Let n8n generate its own encryption key on first boot | The only copy would live in the container volume; losing the volume makes every stored credential unreadable (drill README). |
| Pass the database password to the app via `PGPASSWORD` | `PgVectorStore.__init__` refuses a password embedded in the connection URL, and the entrypoint builds a tmpfs passfile instead; `PGPASSWORD` occurrences in the app env are measured at 0. |

## Evidence

Source:

* `deploy/compose-drill/compose.yaml` — header comment ("Secrets are files, never inline
  values"), the `secrets:` block comment quoting the Compose v2.40.3 warning and naming the
  0700/0644 split, the runner-service comment with the three controls, the app-service
  comment on the tmpfs passfile.
* `deploy/compose-drill/scripts/gen_secrets.sh` — `chmod 700 secrets`, `chmod 644` per file,
  and the comment stating that protection comes from the directory.
* `deploy/compose-drill/app-entrypoint.sh` and
  `src/wozto_ai_reference/pgvector_store.py` (`database_url must not embed a password; use a
  libpq passfile outside the repo`).
* `deploy/compose-drill/DRILL-2026-09-06.md` §6.1 (the ignored `uid`/`gid`/`mode`, the probe
  result, the container uids, the first-run failure) and §7 (the observation table and the
  runner `_FILE` control table).
* `deploy/compose-drill/README.md` — *Secrets*, and *What this drill does NOT prove*.
* `tests/test_compose_secret_contract.py` — the equivalent contract for the repository's own
  root `compose.yaml`.
* Commit `3b3e238` message — "file secrets only", and "Verified by observation, not
  assumption: POSTGRES_PASSWORD / N8N_ENCRYPTION_KEY appear 0 times in container env and
  docker inspect".

Numbers, and where each comes from — all of them are copied from
`DRILL-2026-09-06.md`, which states that every number in it is from one recorded run on a
GitHub Codespace (Ubuntu 24.04, 2 vCPU / 7.76 GiB, Docker 29.7.2, Compose v2.40.3):
directory mode `0700` and file mode `0644` (§6.1 and `gen_secrets.sh`); container uids
999 / 1000 / 10001 (§6.1); probe mount shown as `-rw------- 1 1000 1000` (§6.1);
`POSTGRES_PASSWORD`, `N8N_ENCRYPTION_KEY`, `PGPASSWORD` occurrence counts of 0 (§7);
key sha256 prefix `34cd451e…` matching on both sides (§7); broker silent for 20 s with a
wrong token (§7); `n8nio/runners:2.36.7` and Compose v2.40.3 as the measured versions
(§1, §6.1).

## Related

* ADR 0006 — the healthcheck and restore decisions from the same drill; §6.2 is the direct
  consequence of the permission failure recorded here.
* `deploy/compose-drill/README.md` — the full "proves / does not prove" list.
