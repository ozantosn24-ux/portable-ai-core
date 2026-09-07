#!/usr/bin/env bash
# Bounded load / soak measurement against an ALREADY-RUNNING base drill stack.
#
#   scripts/load.sh prep        # export CA, resolve the drill network, build the load image
#   scripts/load.sh probe       # what does the webhook actually answer to GET and to POST?
#   scripts/load.sh health      # scenario 1: app /health via Caddy TLS, conc 10/50/100
#   scripts/load.sh webhook     # scenario 2: n8n production webhook, conc 5/20/50
#   scripts/load.sh soak        # scenario 3: 10 min mixed (10 conc health + 5 conc webhook)
#   scripts/load.sh all         # prep + probe + health + webhook + soak
#
# It MEASURES. It does not tune the stack, does not edit compose.yaml, and does not
# restart anything.
#
# Why the load runs from a container on the drill network and not from the host:
# the base stack publishes Caddy on 127.0.0.1:18443 only, so the published path is
# reachable from the host loopback but NOT from a container. Running on the drill
# network reaches Caddy on 443 directly, with the same TLS hostnames and the same
# internal CA - and it removes the host port-forward from the measurement path.
# That is a deviation worth knowing about, so it is stated here and in LOAD-*.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRILL_DIR="$(cd "$HERE/.." && pwd)"
OUT="${LOAD_OUT:-$DRILL_DIR/load-out}"
PROJ="${DRILL_PROJECT:-drill}"
IMG="${LOAD_IMAGE:-drill-load:local}"
HTTPX_VERSION="${LOAD_HTTPX_VERSION:-0.28.1}"
PY_IMAGE="${LOAD_PY_IMAGE:-python:3.14-slim}"

dc() { docker compose -p "$PROJ" -f "$DRILL_DIR/compose.yaml" "$@"; }
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

mkdir -p "$OUT"

# ---------------------------------------------------------------------------
# stack probes (cheap, read-only)
# ---------------------------------------------------------------------------
pg_conns()  { dc exec -T postgres psql -U drill_app -At -d drill_app -c "SELECT count(*) FROM pg_stat_activity" 2>/dev/null | tr -d '\r'; }
pg_maxconn(){ dc exec -T postgres psql -U drill_app -At -d drill_app -c "SHOW max_connections" 2>/dev/null | tr -d '\r'; }
n8n_execs() { dc exec -T postgres psql -U drill_app -At -d drill_n8n -c "SELECT count(*) FROM execution_entity" 2>/dev/null | tr -d '\r'; }
n8n_soft()  { dc exec -T postgres psql -U drill_app -At -d drill_n8n -c "SELECT count(*) FROM execution_entity WHERE \"deletedAt\" IS NOT NULL" 2>/dev/null | tr -d '\r'; }
n8n_dbsize(){ dc exec -T postgres psql -U drill_app -At -d drill_n8n -c "SELECT pg_database_size('drill_n8n')" 2>/dev/null | tr -d '\r'; }
pgdata_bytes(){ dc exec -T postgres du -sb /var/lib/postgresql/data 2>/dev/null | cut -f1; }

restarts() {
  local cid name rc
  for cid in $(dc ps -q 2>/dev/null); do
    name="$(docker inspect -f '{{.Name}}' "$cid" | sed 's#^/##')"
    rc="$(docker inspect -f '{{.RestartCount}}' "$cid")"
    printf '%s=%s ' "$name" "$rc"
  done
  echo
}

# mark <phase>  -> one line per snapshot in load-out/markers.tsv
mark() {
  local phase="$1"
  printf '%s\t%s\tpg_conns=%s\tn8n_execs=%s\tn8n_soft_deleted=%s\tn8n_db_bytes=%s\tpgdata_bytes=%s\trestarts=[%s]\tdf_root=%s\n' \
    "$(ts)" "$phase" "$(pg_conns)" "$(n8n_execs)" "$(n8n_soft)" "$(n8n_dbsize)" "$(pgdata_bytes)" \
    "$(restarts)" "$(df -h / | awk 'NR==2{print $3"/"$2" used "$5}')" \
    | tee -a "$OUT/markers.tsv"
}

# ---------------------------------------------------------------------------
# docker stats sampler
# ---------------------------------------------------------------------------
STATS_PID=""
start_stats() { # start_stats <file> <interval_s>
  local f="$1" iv="$2"
  ( while :; do
      local t; t="$(ts)"
      docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.PIDs}}' 2>/dev/null \
        | while IFS= read -r line; do printf '%s\t%s\n' "$t" "$line"; done >> "$f"
      sleep "$iv"
    done ) &
  STATS_PID=$!
}
stop_stats() { [ -n "$STATS_PID" ] && kill "$STATS_PID" 2>/dev/null || true; STATS_PID=""; wait 2>/dev/null || true; }

# ---------------------------------------------------------------------------
# prep
# ---------------------------------------------------------------------------
NET_FILE="$OUT/.net"; CADDY_IP_FILE="$OUT/.caddy_ip"

prep() {
  echo "== prep =="
  local caddy_cid
  caddy_cid="$(dc ps -q caddy)"
  [ -n "$caddy_cid" ] || { echo "caddy is not running - bring the base stack up first" >&2; return 1; }

  dc exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > "$DRILL_DIR/ca.crt"
  [ -s "$DRILL_DIR/ca.crt" ] || { echo "empty CA" >&2; return 1; }
  echo "ca.crt bytes=$(wc -c < "$DRILL_DIR/ca.crt") sha256=$(sha256sum "$DRILL_DIR/ca.crt" | cut -d' ' -f1)"

  docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$caddy_cid" > "$NET_FILE"
  docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$caddy_cid" > "$CADDY_IP_FILE"
  echo "network=$(cat "$NET_FILE") caddy_ip=$(cat "$CADDY_IP_FILE")"

  # Throwaway load image: stock python + one pinned dependency, nothing of ours baked in
  # (load_client.py is bind-mounted at run time, so editing it needs no rebuild).
  docker build -q -t "$IMG" - <<EOF
FROM ${PY_IMAGE}
RUN pip install --no-cache-dir --no-compile httpx==${HTTPX_VERSION}
EOF
  docker run --rm "$IMG" python -c "import httpx,sys;print('load image ok: httpx',httpx.__version__,'python',sys.version.split()[0])"

  echo "max_connections=$(pg_maxconn)"
  mark prep
}

load_net() {
  [ -s "$NET_FILE" ] || { echo "run 'load.sh prep' first" >&2; exit 1; }
  NET="$(cat "$NET_FILE")"; CADDY_IP="$(cat "$CADDY_IP_FILE")"
}

# ---------------------------------------------------------------------------
# one load case
# ---------------------------------------------------------------------------
run_case() { # run_case <label> <url> <method> <conc> <duration_s> [extra docker args...]
  local label="$1" url="$2" method="$3" conc="$4" dur="$5"; shift 5
  echo "-- $(ts) $label  method=$method conc=$conc duration=${dur}s"
  docker run --rm --network "$NET" \
    --add-host "app.drill.internal:${CADDY_IP}" \
    --add-host "n8n.drill.internal:${CADDY_IP}" \
    -v "$HERE/load_client.py:/load_client.py:ro" \
    -v "$DRILL_DIR/ca.crt:/ca.crt:ro" \
    "$@" "$IMG" \
    python /load_client.py --url "$url" --method "$method" --concurrency "$conc" \
      --duration "$dur" --cacert /ca.crt --label "$label" \
    > "$OUT/${label}.json" 2> "$OUT/${label}.stderr" || echo "  (client exited non-zero; see ${label}.stderr)"
  summarise "$OUT/${label}.json" || tail -5 "$OUT/${label}.stderr"
}

# The Codespace host has no python3 (measured 2026-09-07: "python3: command not found"),
# so even the one-line summary is read through the load image rather than the host.
summarise() { # summarise <json file>
  local f="$1"
  [ -s "$f" ] || return 1
  docker run --rm -v "$f:/r.json:ro" "$IMG" python -c '
import json
d = json.load(open("/r.json")); l = d["latency_ms"]
print("  req=%s 2xx=%s err=%s rps=%s p50=%s p95=%s p99=%s max=%s status=%s exc=%s" % (
    d["requests"], d["status_2xx"], d["errors_total"], d["rps"],
    l["p50"], l["p95"], l["p99"], l["max"],
    d["errors_by_status"], d["errors_by_exception"]))'
}

# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------
probe() {
  load_net
  echo "== probe: what does each endpoint answer? =="
  {
    echo "# $(ts)"
    for spec in "GET https://app.drill.internal/health" \
                "GET https://n8n.drill.internal/webhook/drill-ping" \
                "POST https://n8n.drill.internal/webhook/drill-ping"; do
      set -- $spec
      code="$(docker run --rm --network "$NET" \
        --add-host "app.drill.internal:${CADDY_IP}" --add-host "n8n.drill.internal:${CADDY_IP}" \
        -v "$HERE/load_client.py:/load_client.py:ro" -v "$DRILL_DIR/ca.crt:/ca.crt:ro" "$IMG" \
        python -c "
import httpx,sys
r=httpx.request('$1','$2',verify='/ca.crt',timeout=10)
print(r.status_code, len(r.content), repr(r.text[:120]))
" 2>&1 | tail -1)"
      echo "$1 $2 -> $code"
    done
  } | tee "$OUT/probe.txt"
}

health_scenarios() {
  load_net
  echo "== scenario 1: app /health via Caddy TLS =="
  start_stats "$OUT/s1.stats.tsv" 10
  for c in 10 50 100; do
    mark "s1-c${c}-before"
    run_case "s1-health-c${c}" "https://app.drill.internal/health" GET "$c" 30
    mark "s1-c${c}-after"
    sleep 5
  done
  stop_stats
}

webhook_scenarios() {
  load_net
  local method="${WEBHOOK_METHOD:-GET}"
  echo "== scenario 2: n8n production webhook (method=${method}) =="
  start_stats "$OUT/s2.stats.tsv" 10
  for c in 5 20 50; do
    mark "s2-c${c}-before"
    run_case "s2-webhook-c${c}" "https://n8n.drill.internal/webhook/drill-ping" "$method" "$c" 30
    mark "s2-c${c}-after"
    sleep 10   # let n8n drain before the next step; the drain itself is visible in markers
    mark "s2-c${c}-after-drain"
  done
  stop_stats
}

soak() {
  load_net
  local method="${WEBHOOK_METHOD:-GET}" dur="${SOAK_SECONDS:-600}"
  echo "== scenario 3: ${dur}s mixed soak (10 conc /health + 5 conc webhook) =="
  mark "soak-before"
  dc logs --since 1s n8n > /dev/null 2>&1 || true
  local log_mark; log_mark="$(ts)"
  echo "$log_mark" > "$OUT/soak_log_mark.txt"

  start_stats "$OUT/soak.stats.tsv" 30
  ( run_case "soak-health-c10" "https://app.drill.internal/health" GET 10 "$dur" ) &
  local p1=$!
  ( run_case "soak-webhook-c5" "https://n8n.drill.internal/webhook/drill-ping" "$method" 5 "$dur" ) &
  local p2=$!

  # sample the counters on the same 30 s cadence as docker stats
  ( local i=0
    while kill -0 "$p1" 2>/dev/null || kill -0 "$p2" 2>/dev/null; do
      sleep 30; i=$((i+1)); mark "soak-t$((i*30))s" > /dev/null
    done ) &
  local p3=$!

  wait "$p1" "$p2" || true
  kill "$p3" 2>/dev/null || true
  stop_stats
  mark "soak-after"
  sleep 60
  mark "soak-after+60s"

  # pruning: report what n8n logged, do not assume it ran
  dc logs --since "$log_mark" n8n 2>/dev/null \
    | grep -iE 'prun|soft.?delete|hard.?delete' > "$OUT/soak_prune_lines.txt" || true
  echo "prune log lines since soak start: $(wc -l < "$OUT/soak_prune_lines.txt")"
  dc exec -T n8n printenv 2>/dev/null | grep -E '^EXECUTIONS_' | sort > "$OUT/n8n_executions_env.txt" || true
  cat "$OUT/n8n_executions_env.txt"
}

finish() {
  echo "== final state =="
  mark final
  { echo "# $(ts)"; docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'; echo; df -h /; } \
    | tee "$OUT/final_state.txt"
}

case "${1:-all}" in
  prep)    prep ;;
  probe)   probe ;;
  health)  health_scenarios ;;
  webhook) webhook_scenarios ;;
  soak)    soak ;;
  finish)  finish ;;
  all)     prep; probe; health_scenarios; webhook_scenarios; soak; finish ;;
  *)       echo "usage: $0 {prep|probe|health|webhook|soak|finish|all}" >&2; exit 2 ;;
esac
