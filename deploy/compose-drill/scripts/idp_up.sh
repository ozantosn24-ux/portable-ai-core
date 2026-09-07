#!/usr/bin/env bash
# Brings up the identity profile (postgres + keycloak + app) and times it to ALL-HEALTHY.
#
# Only three services start. n8n, the task runner and Caddy are not part of this drill and
# starting them would spend ~1 GB of RAM and ~30 s proving something DRILL-2026-09-06.md
# already proved.
#
# Prints, in this order: secret generation, image pull, build, per-service first-healthy,
# realm import seconds. Every number this prints ends up in S2-DRILL-2026-09-06.md.
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

svc_state_idp() {
  local cid
  cid="$(dci ps -q "$1" 2>/dev/null || true)"
  [ -n "$cid" ] || { echo absent; return; }
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid"
}

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
[ -s secrets/postgres_password.txt ] || scripts/gen_secrets.sh

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

start="$(now_ms)"
dci up -d --no-build "${SERVICES[@]}" > /tmp/drill-idp-up.log 2>&1 &
up_pid=$!

declare -A first_healthy=()
while :; do
  all=1
  for s in "${SERVICES[@]}"; do
    [ -n "${first_healthy[$s]:-}" ] && continue
    st="$(svc_state_idp "$s")"
    if [ "$st" = healthy ]; then
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
    for s in "${SERVICES[@]}"; do echo "  $s = $(svc_state_idp "$s")" >&2; done
    cat /tmp/drill-idp-up.log >&2
    wait "$up_pid" || true
    exit 1
  fi
  sleep 1
done

total="$(secs "$start")"
wait "$up_pid" || { echo "compose up returned non-zero:" >&2; cat /tmp/drill-idp-up.log >&2; exit 1; }
echo "ALL_HEALTHY_SECONDS=${total}"

# ---------------------------------------------------------------- realm import
# Süre, Keycloak'ın KENDİ log'undan okunur — "hazır oldu" ile "realm içe aktarıldı" ayrı
# olaylardır ve ikincisi ölçülmezse import'un çalıştığı yalnız VARSAYILIR.
dci logs keycloak 2>/dev/null | grep -iE "imported|import.*realm|Realm .drill." | tail -5 || true
echo "REALM_PRESENT=$(curl -s -o /dev/null -w '%{http_code}' \
  http://idp.drill.internal:8080/realms/drill/.well-known/openid-configuration)"
