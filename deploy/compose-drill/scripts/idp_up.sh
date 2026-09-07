#!/usr/bin/env bash
# Brings up the identity profile (postgres + keycloak + app) and times it to ALL-HEALTHY.
#
# Only three services start. n8n, the task runner and Caddy are not part of this drill and
# starting them would spend ~1 GB of RAM and ~30 s proving something DRILL-2026-09-06.md
# already proved.
#
# Prints, in this order: secret generation, Keycloak DB objects, image pull, build,
# per-service first-healthy, realm import seconds. Every number this prints ends up in
# S2-DRILL-2026-09-06.md.
#
# ── Two things here exist because of the 2026-09-06 handover drill ──────────────────
#
# 1. **This profile no longer needs a FRESH Postgres volume.** `initdb/*` runs only on an
#    empty PGDATA, so the README's own order (`up.sh` first, then this script) left the
#    volume without the `drill_keycloak` role, the extended healthcheck in
#    compose.idp.yaml correctly refused to go green, and the whole chain died with
#    "dependency failed to start". `ensure_keycloak_db` now creates the role and database
#    idempotently BEFORE the profile starts. It is a no-op when they already exist.
#
# 2. **Failure surfaces in seconds, not at the timeout.** The old loop watched health
#    only; Compose had already given up within seconds, but the script sat out its full
#    900 s ceiling before printing anything. The exit code of `docker compose up` is now
#    the primary signal and `DRILL_UP_TIMEOUT` is only a backstop.
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$DRILL_DIR"

KEYCLOAK_IMAGE="quay.io/keycloak/keycloak:26.7.3"
SERVICES=(postgres keycloak app)
TIMEOUT_S="${DRILL_UP_TIMEOUT:-900}"

dci() {
  docker compose -p "$DRILL_PROJECT" \
    -f "$DRILL_DIR/compose.yaml" -f "$DRILL_DIR/compose.idp.yaml" \
    --profile idp "$@"
}

svc_cid_idp() { dci ps -q "$1" 2>/dev/null || true; }

state_of_cid() {
  [ -n "$1" ] || { echo absent; return; }
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" \
    2>/dev/null || echo absent
}

svc_state_idp() { state_of_cid "$(svc_cid_idp "$1")"; }

# ---------------------------------------------------------------- secrets

mkdir -p secrets
chmod 700 secrets
gen_idp_secret() { # <name> <bytes>
  local target="secrets/$1.txt"
  if [ -s "$target" ]; then echo "keep   $target"; return; fi
  umask 077
  openssl rand -hex "$2" > "$target"
  chmod 644 "$target"   # 0700 dizin + 0644 dosya: gerekçe gen_secrets.sh'de ölçülmüş
  echo "write  $target"
}
gen_idp_secret keycloak_db_password 24
gen_idp_secret keycloak_admin_password 24
gen_idp_secret session_secret 32
# Temel yığının sırları da gerekli (postgres parolası).
# NOTE: called through `bash` on purpose - the scripts are committed with mode 644, so a
# fresh clone cannot execute them directly (measured on a clean clone, 2026-09-07).
[ -s secrets/postgres_password.txt ] || bash scripts/gen_secrets.sh

# ------------------------------------------------- Keycloak role + database (idempotent)
#
# The names come from compose.idp.yaml so this script cannot drift from the compose file.
kc_env() { # <ENV_KEY> -> value, read out of compose.idp.yaml
  awk -v k="$1:" '$1 == k { print $2; exit }' "$DRILL_DIR/compose.idp.yaml"
}
KC_USER="$(kc_env DRILL_KEYCLOAK_USER)"
KC_DB="$(kc_env DRILL_KEYCLOAK_DB)"
[ -n "$KC_USER" ] && [ -n "$KC_DB" ] || {
  echo "cannot read DRILL_KEYCLOAK_USER/DB from compose.idp.yaml" >&2; exit 1; }

# The extended healthcheck in compose.idp.yaml asserts this role by name. If the two ever
# disagree the stack would fail 60 s later with an opaque "unhealthy"; assert it now.
grep -q "rolname = '${KC_USER}'" "$DRILL_DIR/compose.idp.yaml" || {
  echo "compose.idp.yaml healthcheck does not assert role '${KC_USER}'" >&2; exit 1; }

# ⚠️ ONE definition of what Keycloak needs, not two. The statements below are the
# idempotent form of the heredoc in initdb/20-drill-keycloak-db.sh. That file stays
# canonical: its SQL is parsed out and compared against the list this script knows how to
# reproduce, so the two copies cannot diverge silently - if the init script gains a
# statement, this script STOPS instead of quietly creating a half-configured database.
initdb_sql_statements() {
  awk '/<<.SQL./ { f = 1; next } f && /^SQL$/ { f = 0 } f' \
      "$DRILL_DIR/initdb/20-drill-keycloak-db.sh" \
    | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
    | grep -v '^$' | grep -v '^--' | grep -v '^\\'
}
mirrored_statements() {
  cat <<'EOSTMT'
CREATE ROLE :"kc_user" LOGIN PASSWORD :'kc_password';
CREATE DATABASE :"kc_db" OWNER :"kc_user";
REVOKE CONNECT ON DATABASE :"kc_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"kc_db" TO :"kc_user";
EOSTMT
}
assert_initdb_sql_mirrored() {
  if ! diff -u <(mirrored_statements) <(initdb_sql_statements) > /tmp/drill-initdb-sql.diff
  then
    echo "initdb/20-drill-keycloak-db.sh SQL changed; update ensure_keycloak_db():" >&2
    cat /tmp/drill-initdb-sql.diff >&2
    exit 1
  fi
}

# Password handling matches initdb/20-drill-keycloak-db.sh: NEVER an argv value (argv is
# world-readable via /proc/<pid>/cmdline). It is the FIRST LINE of stdin; the shell inside
# the container reads it into the environment and psql picks it up with \getenv, then
# consumes the rest of stdin as its script. No secret on the host argv either.
ensure_keycloak_db() {
  {
    cat "$DRILL_DIR/secrets/keycloak_db_password.txt"
    cat <<'SQL'
\getenv kc_password DRILL_KC_PASSWORD
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'kc_user', :'kc_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'kc_user')
\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'kc_db', :'kc_user')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'kc_db')
\gexec
REVOKE CONNECT ON DATABASE :"kc_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"kc_db" TO :"kc_user";
SQL
  } | dc exec -T postgres sh -c '
        IFS= read -r kc_pw || exit 1
        DRILL_KC_PASSWORD="$kc_pw"
        export DRILL_KC_PASSWORD
        exec psql -X -q -v ON_ERROR_STOP=1 \
             --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
             -v kc_user="$1" -v kc_db="$2"
      ' sh "$KC_USER" "$KC_DB"
}

# Read the objects back from the server afterwards. "psql exited 0" is the command's own
# report; this is the state re-read from the source.
kc_objects_readback() {
  dc exec -T postgres sh -c '
        exec psql -X -At -v ON_ERROR_STOP=1 \
             --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
             -v kc_user="$1" -v kc_db="$2"
      ' sh "$KC_USER" "$KC_DB" <<'SQL'
SELECT 'role=' || (SELECT count(*) FROM pg_roles    WHERE rolname = :'kc_user')
    || ' db='  || (SELECT count(*) FROM pg_database WHERE datname = :'kc_db');
SQL
}

# ⚠️ The gate is "is the container RUNNING", NOT "is it healthy", and that distinction was
# measured on 2026-09-07. Under the idp override an unhealthy Postgres is the NORMAL state
# of a retry: the extended healthcheck asserts the very role this step is about to create,
# so the server is up and accepting connections while its healthcheck says no. Gating on
# health made idp_up.sh refuse to repair the exact situation it exists to repair.
cid_is_running() {
  [ -n "$1" ] || return 1
  [ "$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]
}

assert_initdb_sql_mirrored
pg_cid="$(dc ps -q postgres 2>/dev/null || true)"
pg_state="$(state_of_cid "$pg_cid")"
if [ -z "$pg_cid" ]; then
  # Nothing is running. Either the volume does not exist yet (initdb will do the work when
  # the profile starts, exactly as it did before this fix) or it does - and then the role
  # can only be created against a RUNNING server, so say so instead of failing 60 s later.
  if docker volume inspect "${DRILL_PROJECT}_drill-pgdata" > /dev/null 2>&1; then
    echo "ERROR: the drill Postgres volume (${DRILL_PROJECT}_drill-pgdata) exists but no" >&2
    echo "       postgres container is running, so the Keycloak role cannot be created." >&2
    echo "       Start the base stack first:  bash scripts/up.sh" >&2
    echo "       (or discard the volume:      docker compose -p ${DRILL_PROJECT} down -v)" >&2
    exit 1
  fi
  echo "keycloak-db: fresh volume, initdb/20-drill-keycloak-db.sh will create ${KC_DB}"
elif cid_is_running "$pg_cid"; then
  echo "keycloak-db: postgres is running (health: ${pg_state}); ensuring role + database"
  kc_start="$(now_ms)"
  ensure_keycloak_db
  echo "KEYCLOAK_DB_ENSURE_SECONDS=$(secs "$kc_start")"
  echo "keycloak-db: readback -> $(kc_objects_readback)"
else
  echo "ERROR: the postgres container exists but is not running (state: ${pg_state})," >&2
  echo "       so the Keycloak role cannot be created. Start the base stack:" >&2
  echo "         bash scripts/up.sh" >&2
  echo "       or discard the volume: docker compose -p ${DRILL_PROJECT} down -v" >&2
  exit 1
fi

# ---------------------------------------------------------------- hosts entry
# Issuer TEK bir dizgidir: `idp.drill.internal:8080` hem ağ içinde (servis takma adı) hem
# konakta (bu satır + aynı numaralı yayımlanmış port) çözülmeli.
for host in idp.drill.internal app.drill.internal; do
  if ! grep -qE "^127\.0\.0\.1[[:space:]]+$host\$" /etc/hosts; then
    echo "127.0.0.1 $host" | sudo tee -a /etc/hosts > /dev/null
    echo "hosts  added 127.0.0.1 $host"
  fi
done

# ---------------------------------------------------------------- pull + build

pull_start="$(now_ms)"
docker pull -q "$KEYCLOAK_IMAGE" > /dev/null
echo "KEYCLOAK_PULL_SECONDS=$(secs "$pull_start")"
docker image inspect "$KEYCLOAK_IMAGE" --format 'KEYCLOAK_IMAGE_BYTES={{.Size}}'

# ⚠️ SIRA ÖNEMLİ. `idp/Dockerfile.app-auth` `FROM drill-app:local` ile başlar; o etiket
# daha önceki bir tatbikattan KALMIŞ olabilir ve bu deponun ŞU ANKİ kaynağını taşımaz.
# Temel imaj önce yeniden kurulmazsa, auth katmanı eski bir uygulamanın üzerine biner ve
# tatbikat var olmayan bir kodu "kanıtlar".
base_build_start="$(now_ms)"
dc build app > /tmp/drill-base-build.log 2>&1 || { cat /tmp/drill-base-build.log >&2; exit 1; }
echo "APP_BASE_BUILD_SECONDS=$(secs "$base_build_start")"

build_start="$(now_ms)"
dci build app > /tmp/drill-idp-build.log 2>&1 || { cat /tmp/drill-idp-build.log >&2; exit 1; }
echo "APP_AUTH_BUILD_SECONDS=$(secs "$build_start")"

# ---------------------------------------------------------------- up

UP_LOG=/tmp/drill-idp-up.log
UP_RC=/tmp/drill-idp-up.rc
rm -f "$UP_LOG" "$UP_RC" "$UP_RC.tmp"

pg_cid_before="$(svc_cid_idp postgres)"
kc_cid_before="$(svc_cid_idp keycloak)"

start="$(now_ms)"
# The exit code is written to a sentinel file rather than read with `kill -0`/`wait`,
# because a finished-but-unreaped child still answers `kill -0` and `wait` would block.
# The file appears the instant Compose gives up, which is what the loop below watches for.
#
# ⚠️ `set +e` is LOAD-BEARING, and its absence was measured on 2026-09-07: with errexit
# inherited, a failing `dci up` killed this subshell on the spot, the sentinel was never
# written, and the loop below fell through to the very 900 s wait this rewrite exists to
# remove. The failure looked exactly like the bug being fixed.
( set +e
  dci up -d --no-build "${SERVICES[@]}" > "$UP_LOG" 2>&1
  echo "$?" > "$UP_RC.tmp"; mv "$UP_RC.tmp" "$UP_RC" ) &
up_pid=$!

# Prints WHY, from the three places that actually know: Compose's own output, the service
# table, and the logs of whatever is not healthy. The drill got none of this for 900 s.
fail_fast() { # <reason>
  echo "IDP_UP_FAILED after $(secs "$start")s: $1" >&2
  echo "--- docker compose up output ---" >&2
  tail -n 20 "$UP_LOG" >&2 2>/dev/null || true
  echo "--- docker compose ps ---" >&2
  dci ps --all --format 'table {{.Name}}\t{{.Service}}\t{{.State}}\t{{.Status}}' >&2 2>/dev/null \
    || dci ps --all >&2 2>/dev/null || true
  local s st
  for s in "${SERVICES[@]}"; do
    st="$(svc_state_idp "$s")"
    [ "$st" = healthy ] && continue
    echo "--- last 20 log lines: ${s} (${st}) ---" >&2
    dci logs --tail 20 "$s" >&2 2>/dev/null || true
  done
  exit 1
}

# ⚠️ The container id is carried alongside the timing, because adding this profile to a
# running base stack makes Compose RECREATE postgres and app (new environment, new secret,
# new healthcheck). Without this the loop marks the OLD container healthy and never looks
# again - which is exactly what the drill's failure output shows ("postgres healthy at
# +0.2s" one line above "postgres = unhealthy"). A changed id invalidates the measurement.
declare -A first_healthy=()
declare -A healthy_cid=()
while :; do
  all=1
  for s in "${SERVICES[@]}"; do
    cid="$(svc_cid_idp "$s")"
    if [ -n "${first_healthy[$s]:-}" ] && [ "$cid" != "${healthy_cid[$s]:-}" ]; then
      echo "  ${s} was replaced by Compose; re-measuring"
      unset "first_healthy[$s]"
      unset "healthy_cid[$s]"
    fi
    [ -n "${first_healthy[$s]:-}" ] && continue
    st="$(state_of_cid "$cid")"
    if [ "$st" = healthy ]; then
      first_healthy[$s]="$(secs "$start")"
      healthy_cid[$s]="$cid"
      echo "  ${s} healthy at +${first_healthy[$s]}s"
    else
      all=0
    fi
  done
  # Leaving the loop needs BOTH conditions. "Everything is healthy" alone is not enough
  # when the profile is added to a running stack: measured 2026-09-07, all three services
  # answered `healthy` at +0.6 s from the containers Compose had not replaced yet, so an
  # all-healthy-only exit would have stamped ALL_HEALTHY_SECONDS=0.6 on a stack that was
  # still being rebuilt underneath it.
  if [ "$all" -eq 1 ] && [ -f "$UP_RC" ]; then break; fi

  # PRIMARY SIGNAL. `up -d` waits on the depends_on health conditions, so a dependency
  # failure ("dependency failed to start: container drill-postgres-1 is unhealthy")
  # lands here within seconds instead of at the ceiling.
  if [ -f "$UP_RC" ]; then
    rc="$(cat "$UP_RC")"
    [ "$rc" = 0 ] || fail_fast "docker compose up exited with status ${rc}"
  fi

  # BACKSTOP ONLY: Compose still reports success but something never went healthy.
  elapsed="$(secs "$start")"
  if awk -v e="$elapsed" -v t="$TIMEOUT_S" 'BEGIN { exit !(e > t) }'; then
    fail_fast "TIMEOUT backstop reached (DRILL_UP_TIMEOUT=${TIMEOUT_S}s)"
  fi
  sleep 1
done

total="$(secs "$start")"
wait "$up_pid" || true
rc="$(cat "$UP_RC" 2>/dev/null || echo missing)"
[ "$rc" = 0 ] || fail_fast "docker compose up exited with status ${rc}"
echo "ALL_HEALTHY_SECONDS=${total}"

# ⚠️ Healthy containers and a zero exit code did NOT mean the system worked. Measured
# 2026-09-07: when Compose replaced postgres but kept the running keycloak, that keycloak
# held dead JDBC handles ("PSQLException: This connection has been closed"), reported
# healthy, and every login still failed at the callback. Only idp_check.sh caught it.
if [ -n "$pg_cid_before" ] && [ "$pg_cid_before" != "$(svc_cid_idp postgres)" ] \
   && [ -n "$kc_cid_before" ] && [ "$kc_cid_before" = "$(svc_cid_idp keycloak)" ]; then
  echo "keycloak: postgres was replaced underneath it; restarting to drop dead JDBC handles"
  kc_restart="$(now_ms)"
  dci restart keycloak > /dev/null 2>&1 || fail_fast "keycloak restart failed"
  while [ "$(svc_state_idp keycloak)" != healthy ]; do
    awk -v e="$(secs "$kc_restart")" -v t=180 'BEGIN { exit !(e > t) }' \
      && fail_fast "keycloak did not return to healthy after the post-Postgres restart"
    sleep 2
  done
  echo "KEYCLOAK_RESTART_SECONDS=$(secs "$kc_restart")"
fi

# ---------------------------------------------------------------- realm import
# Süre, Keycloak'ın KENDİ log'undan okunur — "hazır oldu" ile "realm içe aktarıldı" ayrı
# olaylardır ve ikincisi ölçülmezse import'un çalıştığı yalnız VARSAYILIR.
dci logs keycloak 2>/dev/null | grep -iE "imported|import.*realm|Realm .drill." | tail -5 || true
echo "REALM_PRESENT=$(curl -s -o /dev/null -w '%{http_code}' \
  http://idp.drill.internal:8080/realms/drill/.well-known/openid-configuration)"
