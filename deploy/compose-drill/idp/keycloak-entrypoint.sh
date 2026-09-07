#!/bin/bash
# Maps `KC_<OPTION>_FILE` -> `KC_<OPTION>`, because Keycloak does not read file secrets.
#
# MEASURED against quay.io/keycloak/keycloak:26.7.3 in this drill. Every other service in
# this stack takes its secrets as files (`POSTGRES_PASSWORD_FILE`, `N8N_ENCRYPTION_KEY_FILE`,
# `N8N_RUNNERS_AUTH_TOKEN_FILE` — all honoured, all verified by observation). Keycloak is
# the exception, and it fails in two different ways:
#
#   KC_BOOTSTRAP_ADMIN_PASSWORD_FILE -> ignored; the server crash-loops with
#       "bootstrap-admin-username available only when bootstrap admin password is set"
#   KC_DB_PASSWORD_FILE              -> ignored; the server starts and then cannot reach
#       Postgres: "The server requested SCRAM-based authentication, but no password was
#       provided."
#
# ⚠️ CORRECTION, recorded because the wrong reading nearly shipped: `kc.sh show-config`
# prints `kc.db-password-file = ******* (ENV)`, which looks like the option was accepted.
# It was not — show-config echoes the key, it does not prove the option is USED. The
# proof is the connection attempt above. One hit is not a classification.
#
# ⚠️ Honest about what this costs — and MEASURED, because the first version of this note
# was WRONG. Where the value ends up, verified three ways in the drill:
#
#   docker inspect .Config.Env  -> only `KC_*_FILE=/run/secrets/...`. No value. (verified)
#   docker exec <c> env         -> **no value either** (grep count 0). `docker exec` starts
#                                  a new process from the image/Compose environment, so it
#                                  does NOT inherit what this script exported. The earlier
#                                  claim that `docker exec env` would expose it was simply
#                                  wrong, and only measuring caught it.
#   /proc/1/environ             -> `KC_BOOTSTRAP_ADMIN_PASSWORD=` and `KC_DB_PASSWORD=`
#                                  ARE present (grep count 2). This is the real exposure:
#                                  anything that can read PID 1's environment from inside
#                                  the container sees both secrets.
#
# So the file-based secret still keeps the value out of the image, out of `compose.yaml`
# and out of `docker inspect`; what it does not do is keep it out of the process
# environment. That is the whole cost, stated exactly.
set -euo pipefail

for file_var in $(compgen -e | grep -E '^KC_.+_FILE$' || true); do
  target="${file_var%_FILE}"
  path="${!file_var}"
  [ -r "$path" ] || { echo "keycloak-entrypoint: cannot read $file_var -> $path" >&2; exit 1; }
  export "$target=$(cat "$path")"
  # Keycloak bu değişkenleri tanımıyor; ortamda bırakmak yalnız kafa karıştırır.
  unset "$file_var"
done

exec /opt/keycloak/bin/kc.sh "$@"
