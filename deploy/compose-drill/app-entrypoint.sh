#!/bin/sh
# PgVectorStore rejects a password embedded in the connection URL and points at a libpq
# passfile instead. libpq also refuses a passfile that is group/world readable, and a
# Compose file secret is bind-mounted with the host file's mode - which the container
# cannot change. So the password is copied once, at start, into a 0600 file on tmpfs
# owned by the runtime user. Consequences:
#   * no password in the image, in `docker inspect`, or in compose.yaml
#   * the copy lives on tmpfs and dies with the container
set -eu

: "${DRILL_DB_PASSWORD_FILE:?DRILL_DB_PASSWORD_FILE is required}"
: "${DRILL_DB_HOST:=postgres}"
: "${DRILL_DB_PORT:=5432}"
: "${DRILL_DB_NAME:=drill_app}"
: "${DRILL_DB_USER:=drill_app}"

umask 077
passfile=/tmp/pgpass
printf '%s:%s:%s:%s:%s\n' \
  "$DRILL_DB_HOST" "$DRILL_DB_PORT" "$DRILL_DB_NAME" "$DRILL_DB_USER" \
  "$(cat "$DRILL_DB_PASSWORD_FILE")" > "$passfile"
chmod 0600 "$passfile"
export PGPASSFILE="$passfile"

exec "$@"
