#!/usr/bin/env bash
# Exports the CA that Caddy generated for `tls internal` and proves BOTH directions:
#   positive - the chain verifies against that CA
#   negative - the same request WITHOUT the CA fails (otherwise the positive result
#              would only prove that verification was switched off somewhere)
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$DRILL_DIR"

ca="$(export_ca)"
echo "ca_file=${ca} bytes=$(wc -c < "$ca") sha256=$(sha256sum "$ca" | cut -d' ' -f1)"

probe() { # probe <host> <path>
  local host="$1" path="$2" out
  out="$(curl -s -o /dev/null --cacert "$ca" \
        --resolve "${host}:18443:127.0.0.1" \
        -w 'http_code=%{http_code} tls_verify=%{ssl_verify_result} handshake_s=%{time_appconnect} total_s=%{time_total}' \
        "https://${host}:18443${path}" || echo 'CURL_FAILED')"
  echo "  ${host}${path} -> ${out}"
}

echo "== positive: chain must verify against Caddy's internal CA =="
probe app.drill.internal /health
probe n8n.drill.internal /healthz/readiness

echo "== negative control: same request, no CA supplied (must fail) =="
for host in app.drill.internal n8n.drill.internal; do
  if curl -s -o /dev/null --resolve "${host}:18443:127.0.0.1" "https://${host}:18443/" 2>/dev/null; then
    echo "  ${host} -> UNEXPECTED SUCCESS without CA (verification is not being enforced)"
  else
    echo "  ${host} -> failed as expected (exit $?)"
  fi
done
