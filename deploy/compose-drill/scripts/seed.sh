#!/usr/bin/env bash
# Puts something in the stack that a restore can be judged against:
#   * app database: N rows + one marker row + a content digest
#   * n8n: one published workflow (production webhook) + one credential
#     encrypted with the pre-generated key
# Readiness of the workflow is measured as "the production webhook answers 200",
# never as "the port is open".
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$DRILL_DIR"

ROWS="${DRILL_SEED_ROWS:-1000}"
MARKER="DRILL-MARKER-ROW"

psql_app() { dc exec -T postgres psql -U drill_app -v ON_ERROR_STOP=1 -q -d drill_app "$@"; }

echo "== phase 1: seed app database (${ROWS} rows + marker) =="
t0="$(now_ms)"
psql_app -c "DROP TABLE IF EXISTS drill_seed;" \
         -c "CREATE TABLE drill_seed (id integer PRIMARY KEY, payload text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());" \
         -c "INSERT INTO drill_seed (id, payload) SELECT g, 'row-' || g FROM generate_series(1, ${ROWS}) AS g;" \
         -c "INSERT INTO drill_seed (id, payload) VALUES (0, '${MARKER}');"
echo "seed_seconds=$(secs "$t0")"
psql_app -At -c "SELECT count(*) || ' rows, digest ' || md5(string_agg(id::text || ':' || payload, '|' ORDER BY id)) FROM drill_seed;"

# The JSON files arrive through the read-only ./workflows mount declared in compose.yaml,
# not through `docker cp`: with a read-only rootfs the daemon rejects a copy into the
# container ("container rootfs is marked read-only") even for a tmpfs destination.
echo "== phase 2: n8n workflow import + publish =="
t0="$(now_ms)"
dc exec -T n8n n8n import:workflow --input=/drill-workflows/drill-ping.workflow.json
dc exec -T n8n n8n publish:workflow --id=drillping0000001
echo "import_publish_seconds=$(secs "$t0")"

echo "== phase 3: n8n credential import (encrypted with the pre-generated key) =="
dc exec -T n8n n8n import:credentials --input=/drill-workflows/drill-credential.json

echo "== phase 4: production webhook readiness (200, not 'port open') =="
ca="$(export_ca)"
url="https://n8n.drill.internal:18443/webhook/drill-ping"
t0="$(now_ms)"
restarted=0
code=000
for i in $(seq 1 120); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$ca" \
          --resolve n8n.drill.internal:18443:127.0.0.1 "$url" || echo 000)"
  [ "$code" = "200" ] && break
  # A workflow published through the CLI is written straight to the database; the
  # already-running main process does not necessarily register the webhook until it
  # reloads. Measured, not assumed - the restart below is logged when it happens.
  if [ "$i" -eq 20 ] && [ "$restarted" -eq 0 ]; then
    echo "  webhook still ${code} after $(secs "$t0")s -> restarting n8n to reload published workflows"
    dc restart n8n >/dev/null
    restarted=1
  fi
  sleep 1
done
echo "webhook_http_code=${code}"
echo "webhook_ready_seconds=$(secs "$t0")"
echo "webhook_needed_n8n_restart=${restarted}"
[ "$code" = "200" ]
