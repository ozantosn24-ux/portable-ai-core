#!/bin/bash
# Runs once, on an empty data directory, as part of the official image's init phase.
# Creates Keycloak's OWN role and database on the SAME server, then removes the default
# PUBLIC connect grant — same isolation rule as 10-drill-n8n-db.sh, third tenant.
#
# ⚠️ Like every file in this directory, this runs ONLY when PGDATA is empty. Bringing the
# idp profile up on a volume that already exists silently skips it and Keycloak then dies
# with an authentication failure. The postgres healthcheck in compose.idp.yaml asserts the
# role exists for exactly that reason — the n8n half of this stack already learned it the
# expensive way (see the healthcheck note in compose.yaml).
set -euo pipefail

# ⚠️ Parola psql'e KOMUT SATIRI ARGÜMANI olarak verilmez (`-v kc_password=...` DEĞİL).
# Argümanlar `/proc/<pid>/cmdline`da durur ve o dosya konteyner içindeki başka
# kullanıcılara da okunabilir; init sırasında koşan herhangi bir süreç parolayı
# yakalayabilirdi. `\getenv` (psql 16+) değeri ORTAMDAN alır ve ortam
# `/proc/<pid>/environ`dadır — o yalnız aynı kullanıcıya okunur.
# NOT: kardeş `10-drill-n8n-db.sh` hâlâ `-v` yolunu kullanıyor; bu tatbikatın kapsamı
# dışında bırakıldı ve `S2-DRILL-2026-09-06.md §8`de açıkça yazılı.
export DRILL_KC_PASSWORD
DRILL_KC_PASSWORD="$(cat /run/secrets/keycloak_db_password)"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v kc_user="$DRILL_KEYCLOAK_USER" \
     -v kc_db="$DRILL_KEYCLOAK_DB" <<'SQL'
\getenv kc_password DRILL_KC_PASSWORD
CREATE ROLE :"kc_user" LOGIN PASSWORD :'kc_password';
CREATE DATABASE :"kc_db" OWNER :"kc_user";
REVOKE CONNECT ON DATABASE :"kc_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"kc_db" TO :"kc_user";
SQL

echo "initdb: created database ${DRILL_KEYCLOAK_DB} owned by ${DRILL_KEYCLOAK_USER}; PUBLIC connect revoked"
