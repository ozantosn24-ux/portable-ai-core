#!/bin/bash
# Runs once, on an empty data directory, as part of the official image's init phase.
# Creates Keycloak's OWN role and database on the SAME server, then removes the default
# PUBLIC connect grant — same isolation rule as 10-drill-n8n-db.sh, third tenant.
#
# ⚠️ Like every file in this directory, this runs ONLY when PGDATA is empty. Bringing the
# idp profile up on a volume that already exists silently skips it, so
# `scripts/idp_up.sh` creates the role and database idempotently before starting the
# profile. The postgres healthcheck in compose.idp.yaml asserts the role exists — the n8n
# half of this stack already learned it the expensive way (see the note in compose.yaml).
#
# ⚠️ THIS FILE IS MOUNTED INTO THE BASE STACK TOO, where `keycloak_db_password` is NOT a
# secret and `DRILL_KEYCLOAK_*` are NOT set. Until 2026-09-07 it aborted there, and the
# damage was invisible (measured, three fresh base-only bring-ups):
#
#     cat: /run/secrets/keycloak_db_password: No such file or directory
#     PostgreSQL Database directory appears to contain a database; Skipping initialization
#
# initialisation aborted, the container died, `restart: unless-stopped` brought it back,
# the second boot found a non-empty PGDATA and skipped init entirely — and the stack then
# reported all six services healthy, because the base healthcheck only asserts the
# `drill_n8n` role that the earlier `10-` script had already created. It was also a RACE:
# three of four fresh bring-ups won it, one lost and failed outright with
# `dependency failed to start: container drill-postgres-1 is unhealthy`.
#
# So the absent secret is now a SKIP, not a failure. Doing nothing here is correct: the
# base stack has no Keycloak to serve, and the idp path is covered by scripts/idp_up.sh.
set -euo pipefail

# ⚠️ Guarded with an `if`, NOT an early `exit 0`. The official entrypoint SOURCES a
# non-executable init file (`docker-entrypoint.sh: sourcing …` — this file is committed
# mode 644, so that is the path actually taken), and in a sourced file `exit` terminates
# the ENTRYPOINT itself, half-way through initialisation. An `if` behaves identically
# whether the file is sourced or executed.
if [ ! -r /run/secrets/keycloak_db_password ] \
   || [ -z "${DRILL_KEYCLOAK_USER:-}" ] || [ -z "${DRILL_KEYCLOAK_DB:-}" ]; then
  echo "initdb: keycloak secret not mounted; idp role/db will be ensured by scripts/idp_up.sh"
else

# ⚠️ Parola psql'e KOMUT SATIRI ARGÜMANI olarak verilmez (`-v kc_password=...` DEĞİL).
# Argümanlar `/proc/<pid>/cmdline`da durur ve o dosya konteyner içindeki başka
# kullanıcılara da okunabilir; init sırasında koşan herhangi bir süreç parolayı
# yakalayabilirdi. `\getenv` (psql 16+) değeri ORTAMDAN alır ve ortam
# `/proc/<pid>/environ`dadır — o yalnız aynı kullanıcıya okunur.
# NOT: kardeş `10-drill-n8n-db.sh` hâlâ `-v` yolunu kullanıyor; bu tatbikatın kapsamı
# dışında bırakıldı ve `S2-DRILL-2026-09-06.md §8`de açıkça yazılı.
export DRILL_KC_PASSWORD
DRILL_KC_PASSWORD="$(cat /run/secrets/keycloak_db_password)"

# ⚠️ The SQL below is parsed out of this file by scripts/idp_up.sh, which mirrors it in an
# idempotent form for volumes that initdb never ran against. Change a statement here and
# that script STOPS until its mirror is updated — there is one definition, not two.
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
fi
