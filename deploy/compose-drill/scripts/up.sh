#!/usr/bin/env bash
# Brings the stack up and times it to ALL-HEALTHY - not to "containers created".
# Prints the first moment each service reported healthy, and the wall clock total.
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$DRILL_DIR"
require_secrets

SERVICES=(postgres app caddy n8n n8n-runner)
TIMEOUT_S="${DRILL_UP_TIMEOUT:-900}"

build_start="$(now_ms)"
dc build
build_s="$(secs "$build_start")"
echo "build: ${build_s}s"

start="$(now_ms)"
dc up -d --no-build > /tmp/drill-up.log 2>&1 &
up_pid=$!

declare -A first_healthy=()
while :; do
  all=1
  for s in "${SERVICES[@]}"; do
    [ -n "${first_healthy[$s]:-}" ] && continue
    st="$(svc_state "$s")"
    # services that declare a healthcheck report health; n8n-runner reports state
    if [ "$st" = healthy ] || { [ "$s" = n8n-runner ] && [ "$st" = running ]; }; then
      first_healthy[$s]="$(secs "$start")"
      echo "  ${s} healthy at +${first_healthy[$s]}s"
    else
      all=0
    fi
  done
  [ "$all" -eq 1 ] && break
  elapsed="$(secs "$start")"
  if awk -v e="$elapsed" -v t="$TIMEOUT_S" 'BEGIN { exit !(e > t) }'; then
    echo "TIMEOUT after ${elapsed}s; states:" >&2
    for s in "${SERVICES[@]}"; do echo "  $s = $(svc_state "$s")" >&2; done
    cat /tmp/drill-up.log >&2
    wait "$up_pid" || true
    exit 1
  fi
  sleep 1
done

total="$(secs "$start")"
wait "$up_pid" || { echo "compose up returned non-zero:" >&2; cat /tmp/drill-up.log >&2; exit 1; }
echo "ALL_HEALTHY_SECONDS=${total}"
echo "BUILD_SECONDS=${build_s}"
