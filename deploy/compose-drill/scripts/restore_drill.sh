#!/usr/bin/env bash
# Restore drill: a SECOND compose project, a FRESH postgres volume, the dumps and the
# separately backed-up encryption key. Nothing here reuses the live stack's data.
#
# What it proves, phase by phase, with elapsed seconds for each:
#   A  a fresh Postgres comes up healthy (initdb re-creates both roles)
#   B  both dumps restore
#   C  the app and n8n start against the restored data with the BACKED-UP key
#   D  row count + content digest match the manifest, the published workflow is present,
#      and a credential decrypts with that key. The decryption check is the one that
#      actually tests the key: a fresh n8n starts happily with the WRONG key and only
#      fails later, when a credential is used.
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$DRILL_DIR"

BK="${1:-$(ls -1d backups/*/ 2>/dev/null | sort | tail -1)}"
BK="${BK%/}"
if [ ! -s "$BK/manifest.json" ]; then
  echo "no manifest.json under: $BK" >&2
  exit 1
fi
TS="$(basename "$BK")"
KEYDIR="key-backups/$TS"
if [ ! -s "$KEYDIR/n8n_encryption_key.txt" ]; then
  echo "no key backup under: $KEYDIR" >&2
  exit 1
fi

RP=drill-restore
rdc() { docker compose -p "$RP" -f "$DRILL_DIR/compose.yaml" "$@"; }

# The restore stack gets its own secrets directory: the same database passwords, but the
# encryption key comes from the KEY BACKUP, which is the artifact under test.
#
# 0644 files inside a 0700 directory, for the reason measured in gen_secrets.sh: Compose
# ignores `mode`/`uid` on file secrets and mounts them with the host's permissions, and
# the container users differ (postgres 999, n8n 1000, app 10001). The first attempt here
# used 0600 and the restore stack's Postgres went straight to `unhealthy`.
rm -rf restore-secrets
mkdir -p restore-secrets
chmod 700 restore-secrets
install -m 644 secrets/postgres_password.txt restore-secrets/
install -m 644 secrets/n8n_db_password.txt restore-secrets/
install -m 644 secrets/runner_auth_token.txt restore-secrets/
install -m 644 "$KEYDIR/n8n_encryption_key.txt" restore-secrets/
export DRILL_SECRETS_DIR=./restore-secrets

expected_rows="$(grep -o '"app_rows": [0-9]*' "$BK/manifest.json" | head -1 | awk '{print $2}')"
expected_digest="$(grep -o '"app_digest": "[^"]*"' "$BK/manifest.json" | head -1 | cut -d'"' -f4)"

total_start="$(now_ms)"

echo "== phase A: fresh Postgres (empty volume) =="
t0="$(now_ms)"
rdc down -v --remove-orphans >/dev/null 2>&1 || true
rdc up -d postgres >/dev/null
st=none
for _ in $(seq 1 120); do
  cid="$(rdc ps -q postgres)"
  st="$(docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo none)"
  if [ "$st" = healthy ]; then
    break
  fi
  sleep 1
done
if [ "$st" != healthy ]; then
  echo "postgres never became healthy (state=$st)" >&2
  exit 1
fi
echo "phaseA_fresh_postgres_seconds=$(secs "$t0")"

echo "== phase B: pg_restore both databases =="
# MEASURED: do NOT pass --no-owner/--no-privileges here. The first attempt did, pg_restore
# exited 0, and all 129 n8n tables came back owned by the superuser drill_app instead of
# drill_n8n. n8n then connected as drill_n8n, failed its migrations on a relation it could
# not touch, and crash-looped. A restore that exits 0 is not a restore that works: the roles
# are re-created by the init script, so let the dump put ownership back where it belongs.
t0="$(now_ms)"
rdc exec -T postgres pg_restore -U drill_app -d drill_app < "$BK/drill_app.dump"
rdc exec -T postgres pg_restore -U drill_app -d drill_n8n < "$BK/drill_n8n.dump"
echo "phaseB_pg_restore_seconds=$(secs "$t0")"

echo "== phase C: app + n8n against restored data, using the backed-up key =="
t0="$(now_ms)"
rdc up -d app n8n n8n-runner >/dev/null
a=none
n=none
for _ in $(seq 1 180); do
  a="$(docker inspect --format '{{.State.Health.Status}}' "$(rdc ps -q app)" 2>/dev/null || echo none)"
  n="$(docker inspect --format '{{.State.Health.Status}}' "$(rdc ps -q n8n)" 2>/dev/null || echo none)"
  if [ "$a" = healthy ] && [ "$n" = healthy ]; then
    break
  fi
  sleep 1
done
echo "phaseC_services_seconds=$(secs "$t0") app=${a} n8n=${n}"

echo "== phase D: verification =="
t0="$(now_ms)"
rows="$(rdc exec -T postgres psql -U drill_app -At -d drill_app -c 'SELECT count(*) FROM drill_seed;')"
digest="$(rdc exec -T postgres psql -U drill_app -At -d drill_app -c "SELECT md5(string_agg(id::text || ':' || payload, '|' ORDER BY id)) FROM drill_seed;")"
marker="$(rdc exec -T postgres psql -U drill_app -At -d drill_app -c 'SELECT payload FROM drill_seed WHERE id = 0;')"
echo "rows=${rows} expected_rows=${expected_rows}"
if [ "$digest" = "$expected_digest" ]; then
  echo "digest_match=yes"
else
  echo "digest_match=NO  restored=${digest}  expected=${expected_digest}"
fi
echo "marker_row=${marker}"
echo "-- table ownership in the restored n8n database (must be drill_n8n, not the superuser):"
rdc exec -T postgres psql -U drill_app -At -d drill_n8n -c "SELECT tableowner || ' owns ' || count(*) || ' tables' FROM pg_tables WHERE schemaname = 'public' GROUP BY tableowner;"

# Asked of the database, not of the CLI: with N8N_LOG_FORMAT=json the output of
# `n8n list:workflow` arrives as a JSON *log record*, so any pipeline that filters out
# info-level lines silently throws the answer away. (It cost one confusing blank section
# in this drill before it was noticed.)
echo "-- workflows in the restored database (id|name|published):"
rdc exec -T postgres psql -U drill_app -At -d drill_n8n -c 'SELECT id || $$|$$ || name || $$|$$ || active FROM workflow_entity;'

echo "-- readiness of the restored n8n (in-container; this stack has no proxy):"
rdc exec -T n8n wget -q -O - http://127.0.0.1:5678/healthz/readiness || echo "readiness FAILED"
echo

echo "-- credential decryption with the backed-up key:"
if rdc exec -T n8n sh -c 'n8n export:credentials --all --decrypted --output=/tmp/cred-check.json >/dev/null 2>&1 && grep -q drill-fake-not-a-secret /tmp/cred-check.json && rm -f /tmp/cred-check.json'; then
  echo "credential_decrypt=OK (marker value recovered from the encrypted row)"
else
  echo "credential_decrypt=FAILED"
fi
echo "phaseD_verify_seconds=$(secs "$t0")"

echo "restore_total_seconds=$(secs "$total_start")"

echo "== teardown of the restore stack =="
rdc down -v --remove-orphans >/dev/null
rm -rf restore-secrets
echo "restore stack removed"
