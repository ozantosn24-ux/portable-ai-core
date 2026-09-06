#!/bin/bash
# Runs once, on an empty data directory, as part of the official image's init phase.
# Creates n8n's OWN role and database, then removes the default PUBLIC connect grant so
# the two applications cannot read each other's database.
set -euo pipefail

n8n_password="$(cat /run/secrets/n8n_db_password)"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v n8n_user="$DRILL_N8N_USER" \
     -v n8n_db="$DRILL_N8N_DB" \
     -v app_db="$POSTGRES_DB" \
     -v n8n_password="$n8n_password" <<'SQL'
CREATE ROLE :"n8n_user" LOGIN PASSWORD :'n8n_password';
CREATE DATABASE :"n8n_db" OWNER :"n8n_user";
REVOKE CONNECT ON DATABASE :"app_db" FROM PUBLIC;
REVOKE CONNECT ON DATABASE :"n8n_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"n8n_db" TO :"n8n_user";
SQL

echo "initdb: created database ${DRILL_N8N_DB} owned by ${DRILL_N8N_USER}; PUBLIC connect revoked on both databases"
