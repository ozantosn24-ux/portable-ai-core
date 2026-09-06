# Shared helpers. Sourced, not executed.
DRILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRILL_PROJECT="${DRILL_PROJECT:-drill}"

now_ms() { date +%s%3N; }

# secs <start_ms> [end_ms]  -> seconds with one decimal
secs() {
  local a="$1" b="${2:-$(now_ms)}"
  awk -v a="$a" -v b="$b" 'BEGIN { printf "%.1f", (b - a) / 1000 }'
}

dc() { docker compose -p "$DRILL_PROJECT" -f "$DRILL_DIR/compose.yaml" "$@"; }

# Container health (or plain state when the service declares no healthcheck).
svc_state() {
  local cid
  cid="$(dc ps -q "$1" 2>/dev/null || true)"
  [ -n "$cid" ] || { echo absent; return; }
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid"
}

# Caddy signs with a CA it generates itself; the drill trusts THAT file, never -k.
export_ca() {
  local out="${1:-$DRILL_DIR/ca.crt}"
  dc exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > "$out"
  [ -s "$out" ] || { echo "export_ca: empty CA file" >&2; return 1; }
  echo "$out"
}

require_secrets() {
  local d="${DRILL_SECRETS_DIR:-$DRILL_DIR/secrets}" f
  for f in postgres_password n8n_db_password n8n_encryption_key runner_auth_token; do
    [ -s "$d/$f.txt" ] || { echo "missing secret $d/$f.txt - run scripts/gen_secrets.sh" >&2; return 1; }
  done
}
