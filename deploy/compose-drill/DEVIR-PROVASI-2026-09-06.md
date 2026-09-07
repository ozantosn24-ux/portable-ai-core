# Devir Provası — compose-drill (2026-09-06)

Newcomer handover drill: bring up `deploy/compose-drill` using only its README, inside a
throwaway GitHub Codespace, following the "Run it" block literally and in order.

**Environment:** GitHub Codespace `deploy-lab-q7w9xvr7rwqwh64wv`, Ubuntu 24.04, Docker
29.7.2, Compose v2.40.3, 2 vCPU / 7.8 GiB RAM, overlay root (32G, ~20G free at start).
Repo `ozantosn24-ux/portable-ai-core`, pinned commit `69100a1a3601210fc94f55c3afed1a23dddd3f5b`,
cloned fresh into `/workspaces/handover`. Pre-existing container `wozto-control-plane` was
present and untouched throughout. Drill window: 2026-09-07 01:13:04 UTC → 01:36:02 UTC.

## Step-by-step

| Step | Seconds | First-try | Stuck? | What unblocked me | README verdict |
|---|---|---|---|---|---|
| `gen_secrets.sh` | 0 | Yes | No | — | Covered |
| `up.sh` | 64 (script: build 2.8s, all-healthy 60.9s) | Yes | No | — | Covered |
| `seed.sh` | 44 | Yes | No — but the script itself needed to restart `n8n` mid-run to make the published workflow's webhook return 200 (`webhook_needed_n8n_restart=1`), self-healed, no action from me | — | Covered enough (RC=0), but the restart-on-publish behavior isn't mentioned anywhere in the README |
| `verify_tls.sh` | 0 (<1) | Yes | No | — | Covered |
| `backup.sh` | 4 (script: 4.9) | Yes | No | — | Covered |
| `restore_drill.sh` | 36 (script: 34.8) | Yes | No | — | Covered |
| `idp_up.sh` | 909 (script's own `TIMEOUT after 900.3s`) | **No** — exit 1 | **Yes** | Had to read `compose.idp.yaml`'s header comment (lines 32-35) and `idp_up.sh` source, since the step failed | **Missing** — nothing in the README says the idp profile needs a *fresh* Postgres volume, or that a failure here can take 15 minutes to surface |
| `idp_check.sh` | 0 | No — exit 7 (connection failure) | No (direct, fully-explained fallout of the step above; keycloak/app never started) | — | Missing — no note that this script has a hard dependency on `idp_up.sh` having actually reached healthy |
| Clean-up (`down -v` ×2 + delete `secrets/*.txt` + revert `/etc/hosts`) | ~10 for the two `down -v` calls | Partial — the two `down -v` commands worked first try; my `sed -i` on `/etc/hosts` did not | **Yes** (the hosts edit) | Rewrote via `grep -v ... | sudo tee` + `cp` instead of `sed -i` (in-place rename fails with "Device or resource busy" on this container's bind-mounted `/etc/hosts`) | Not covered at all — the README's "Run it" block only lists the two `down -v` lines; it never mentions that `idp_up.sh` writes to `secrets/` and `/etc/hosts` and that a clean re-run needs those undone |

**Exact error text, `idp_up.sh`:**
```
postgres healthy at +0.2s
app healthy at +0.5s
TIMEOUT after 900.3s; states:
  postgres = unhealthy
  keycloak = absent
  app = absent
...
dependency failed to start: container drill-postgres-1 is unhealthy
```
Root cause (confirmed by reading `compose.idp.yaml`, not the README): the idp profile
extends Postgres's healthcheck to require a `drill_keycloak` role, created only by
`initdb/20-drill-keycloak-db.sh` — which Postgres only ever runs against an *empty*
`PGDATA`. Because I had already run `up.sh` (and `restore_drill.sh`) before `idp_up.sh`
— exactly the order the README's single "Run it" code block puts them in — the volume was
already initialized without that role, so Postgres could never pass health and the whole
chain (`keycloak`, `app`) stayed `Created`/`absent`. The script's own health-poll loop
doesn't inspect its background `docker compose up` process for an early failure, either —
the real Compose error appears within seconds but is only surfaced after the fixed 900s
`DRILL_UP_TIMEOUT` regardless.

**Exact error, `idp_check.sh`:** first line only, `== login as alice ==`, then exit 7
(curl connection failure) — immediate, direct consequence of the above.

## README fixes I would make

1. In the "Run it" block, immediately before `scripts/idp_up.sh`, current text:
   > `scripts/idp_up.sh               # 3 more secret files, /etc/hosts entries, --profile idp up`

   Replace with a line above it: *"idp requires a **fresh** Postgres volume — `initdb/*`
   only runs once. If you already ran `up.sh` (or `restore_drill.sh`), run
   `docker compose -p drill down -v` first, or Postgres will never pass its extended
   healthcheck and `idp_up.sh` will spend its full 900s `DRILL_UP_TIMEOUT` before failing."*

2. Current cleanup block:
   ```
   docker compose -p drill down -v                 # clean up (base stack)
   docker compose -p drill --profile idp down -v   # clean up when the idp profile was used
   ```
   Add two lines: `rm -f secrets/*.txt` (keep the `.example` files) and a note that
   `idp_up.sh` appended `idp.drill.internal` / `app.drill.internal` to `/etc/hosts` and
   that a plain `sed -i` may fail with "Device or resource busy" on a container whose
   `/etc/hosts` is a bind mount — use `grep -v drill.internal /etc/hosts | sudo tee ...`
   instead.

3. Architecture diagram line:
   > `│ n8n-runner │ (external task runners, 5679)`

   `docker compose -p drill --profile idp ps -a` shows `drill-n8n-runner-1` publishing
   `5680/tcp`, not 5679. Fix the number (or explain what 5679 actually refers to, if it's
   intentional and different from the container port).

4. Under "What this drill proves" / Requirements, the claim *"Host ports used by the base
   stack: `127.0.0.1:18080` and `127.0.0.1:18443` only, and Postgres is not reachable from
   the host at all"* has no accompanying verification command. Add one
   (e.g. `docker compose -p drill port postgres` should error, `ss -ltnp | grep -E '18080|18443'`)
   so a newcomer can check the claim instead of trusting it.

## Promised but not observed

- Base-stack host port restriction (`18080`/`18443` only, Postgres unreachable) — never
  independently checked during the up window.
- idp profile's promised host ports `127.0.0.1:8080` (Keycloak) and `127.0.0.1:8000`
  (API) — never observed; the idp stack never reached healthy.
- `idp_check.sh`'s promised behavior (alice/bob/mia logins, cross-user 403s, ledger rows)
  — never observed for the same reason.
- Keycloak's documented secret-leak exception (`/proc/1/environ`) — never independently
  verified; the Keycloak container never started (stuck at `Created`).

## Observed, not mentioned by README

- `verify_tls.sh` writes `ca.crt` into the `compose-drill/` working directory — not in the
  README's file/piece list.
- `seed.sh` silently restarts the `n8n` container mid-run to make a just-published
  workflow's webhook live.
- The idp-profile Postgres healthcheck's fresh-volume requirement is documented only in a
  YAML comment in `compose.idp.yaml`, not in the README or in `S2-DRILL-2026-09-06.md`
  (which the README explicitly points to for idp-profile detail).
- Two locally built images (`drill-app:local`, `drill-app-auth:local`, ~433MB combined)
  survive the README's cleanup instructions, which never mention `docker rmi`.

**Total wall time: 1378s (22m58s). Total stuck events: 2** (idp profile bring-up requiring
source investigation; `/etc/hosts` in-place edit failing on a bind-mounted file during
clean-up).
