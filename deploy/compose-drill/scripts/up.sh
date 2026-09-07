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

UP_LOG=/tmp/drill-up.log
UP_RC=/tmp/drill-up.rc
rm -f "$UP_LOG" "$UP_RC" "$UP_RC.tmp"

start="$(now_ms)"
# ⚠️ The exit code of `docker compose up` is the PRIMARY signal; DRILL_UP_TIMEOUT is only a
# backstop. Measured 2026-09-07: when Postgres lost its start-up race Compose gave up after
# ~10 s with "dependency failed to start: container drill-postgres-1 is unhealthy", every
# other service stayed `Created` — and this loop, which watched health only, spun toward its
# full 900 s ceiling without ever printing the reason. `scripts/idp_up.sh` carried the
# identical bug and cost a handover drill 15 minutes.
#
# `set +e` is LOAD-BEARING: with errexit inherited, a failing `dc up` kills this subshell
# before it records the code, the sentinel never appears, and the symptom is exactly the
# bug above.
( set +e
  dc up -d --no-build > "$UP_LOG" 2>&1
  echo "$?" > "$UP_RC.tmp"; mv "$UP_RC.tmp" "$UP_RC" ) &
up_pid=$!

fail_fast() { # <reason>
  echo "UP_FAILED after $(secs "$start")s: $1" >&2
  echo "--- docker compose up output ---" >&2
  tail -n 20 "$UP_LOG" >&2 2>/dev/null || true
  echo "--- docker compose ps ---" >&2
  dc ps --all --format 'table {{.Name}}\t{{.Service}}\t{{.State}}\t{{.Status}}' >&2 2>/dev/null \
    || dc ps --all >&2 2>/dev/null || true
  local s st
  for s in "${SERVICES[@]}"; do
    st="$(svc_state "$s")"
    [ "$st" = healthy ] && continue
    echo "--- last 20 log lines: ${s} (${st}) ---" >&2
    dc logs --tail 20 "$s" >&2 2>/dev/null || true
  done
  exit 1
}

declare -A first_healthy=()
declare -A healthy_cid=()
while :; do
  all=1
  for s in "${SERVICES[@]}"; do
    cid="$(dc ps -q "$s" 2>/dev/null || true)"
    # A recreate invalidates an earlier measurement: never report a container Compose has
    # already replaced as the one that went healthy.
    if [ -n "${first_healthy[$s]:-}" ] && [ "$cid" != "${healthy_cid[$s]:-}" ]; then
      echo "  ${s} was replaced by Compose; re-measuring"
      unset "first_healthy[$s]"
      unset "healthy_cid[$s]"
    fi
    [ -n "${first_healthy[$s]:-}" ] && continue
    st="$(svc_state "$s")"
    # services that declare a healthcheck report health; n8n-runner reports state
    if [ "$st" = healthy ] || { [ "$s" = n8n-runner ] && [ "$st" = running ]; }; then
      first_healthy[$s]="$(secs "$start")"
      healthy_cid[$s]="$cid"
      echo "  ${s} healthy at +${first_healthy[$s]}s"
    else
      all=0
    fi
  done

  # Leaving needs BOTH: everything healthy AND Compose finished. "All healthy" on its own
  # can be true of containers Compose is about to replace.
  if [ "$all" -eq 1 ] && [ -f "$UP_RC" ]; then break; fi

  if [ -f "$UP_RC" ]; then
    rc="$(cat "$UP_RC")"
    [ "$rc" = 0 ] || fail_fast "docker compose up exited with status ${rc}"
  fi

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
echo "BUILD_SECONDS=${build_s}"
