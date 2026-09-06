#!/usr/bin/env bash
# Generates every secret the stack needs, once.
#
# The n8n encryption key is generated HERE, before n8n ever starts. If n8n generates it
# itself on first boot, the only copy lives inside the container volume - and the first
# time that volume is lost, every stored credential is unrecoverable.
#
# MEASURED IN THIS DRILL (compose v2.40.3, non-swarm): Compose mounts a file secret with
# the HOST file's owner and mode, and says so out loud:
#   "secrets `uid`, `gid` and `mode` are not supported, they will be ignored"
# The containers do not share a uid - postgres runs as 999, n8n and the runner as 1000,
# the app image as 10001 - so 0600 files owned by the deploy user are unreadable to most
# of them. The first run of this drill died exactly there:
#   cat: /run/secrets/n8n_db_password: Permission denied
# So the protection comes from the DIRECTORY (0700, deploy user only) and the files
# inside are 0644, readable by every container user but by no other host user.
set -euo pipefail
cd "$(dirname "$0")/.."

force=0
[ "${1:-}" = "--force" ] && force=1

mkdir -p secrets
chmod 700 secrets

gen() { # gen <name> <bytes>
  local target="secrets/$1.txt"
  if [ -s "$target" ] && [ "$force" -eq 0 ]; then
    echo "keep   $target (already exists; --force to regenerate)"
    return
  fi
  umask 077
  openssl rand -hex "$2" > "$target"
  chmod 644 "$target"
  echo "write  $target ($2 bytes of entropy, hex-encoded; 0644 inside a 0700 directory)"
}

gen postgres_password 24
gen n8n_db_password 24
gen n8n_encryption_key 32
gen runner_auth_token 32

# No .env is written, on purpose. An earlier version of this script mirrored the runner
# token into .env because the n8n docs state that the runner image ignores *_FILE
# variables. Measured against 2.36.7 that is not true - the runner does read
# N8N_RUNNERS_AUTH_TOKEN_FILE - so the plaintext second copy was deleted instead of kept
# "just in case". The three controls behind that claim are in compose.yaml.

echo
echo "sha256 fingerprints (values themselves are never printed):"
sha256sum secrets/*.txt | sed 's#secrets/##'
