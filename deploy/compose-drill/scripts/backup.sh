#!/usr/bin/env bash
# Nightly-shaped backup of the whole stack state.
#
# Two rules this script exists to enforce:
#   1. The n8n encryption key is copied to a location SEPARATE from the database dumps.
#      A backup that stores both together means one stolen archive yields both the
#      ciphertext and the key that opens it.
#   2. Integrity is hash + size, not "the file exists". manifest.json records sha256 and
#      byte size for every artifact, plus the row count and content digest that the
#      restore has to reproduce.
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$DRILL_DIR"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BK="backups/$TS"
KEYDIR="key-backups/$TS"
mkdir -p "$BK" "$KEYDIR"
chmod 700 "$KEYDIR"

psql_q() { dc exec -T postgres psql -U drill_app -At -d "$1" -c "$2"; }

total_start="$(now_ms)"

echo "== phase 1: pg_dump (custom format) =="
t0="$(now_ms)"
dc exec -T postgres pg_dump -U drill_app -Fc -d drill_app > "$BK/drill_app.dump"
dc exec -T postgres pg_dump -U drill_app -Fc -d drill_n8n > "$BK/drill_n8n.dump"
dump_s="$(secs "$t0")"
# A custom-format dump starts with the literal magic PGDMP. Checking it here catches a
# stream that was mangled on the way to the host now, instead of at restore time.
for f in "$BK/drill_app.dump" "$BK/drill_n8n.dump"; do
  magic="$(head -c 5 "$f")"
  if [ "$magic" != "PGDMP" ]; then
    echo "FATAL: $f is not a custom-format dump (magic=$magic)" >&2
    exit 1
  fi
done
echo "pg_dump_seconds=${dump_s}"

echo "== phase 2: n8n workflow export =="
t0="$(now_ms)"
wf_count_db="$(psql_q drill_n8n 'SELECT count(*) FROM workflow_entity;')"
if [ "${wf_count_db:-0}" -gt 0 ]; then
  # --published exports the published version, not whatever draft happens to be current.
  # The export lands on the container's tmpfs and comes out as a tar stream rather than
  # via `docker cp`, which the daemon refuses for a container with a read-only rootfs.
  mkdir -p "$BK/workflows"
  dc exec -T n8n rm -rf /tmp/wf-export
  dc exec -T n8n mkdir -p /tmp/wf-export
  dc exec -T n8n n8n export:workflow --all --published --separate --output=/tmp/wf-export/
  dc exec -T n8n tar -C /tmp/wf-export -cf - . | tar -xf - -C "$BK/workflows"
else
  echo "no workflows in database - export skipped"
  mkdir -p "$BK/workflows"
fi
export_s="$(secs "$t0")"
echo "n8n_export_seconds=${export_s} workflow_rows_in_db=${wf_count_db}"

echo "== phase 3: encryption key -> SEPARATE location =="
install -m 600 secrets/n8n_encryption_key.txt "$KEYDIR/n8n_encryption_key.txt"
echo "key_backup=${KEYDIR}/n8n_encryption_key.txt (kept outside ${BK})"

echo "== phase 4: content checks =="
app_rows="$(psql_q drill_app 'SELECT count(*) FROM drill_seed;')"
app_digest="$(psql_q drill_app "SELECT md5(string_agg(id::text || ':' || payload, '|' ORDER BY id)) FROM drill_seed;")"
cred_rows="$(psql_q drill_n8n 'SELECT count(*) FROM credentials_entity;')"
echo "app_rows=${app_rows} app_digest=${app_digest} n8n_credentials=${cred_rows}"

echo "== phase 5: manifest =="
manifest="$BK/manifest.json"
{
  printf '{\n'
  printf '  "created_utc": "%s",\n' "$TS"
  printf '  "stack": "compose project drill",\n'
  printf '  "checks": { "app_rows": %s, "app_digest": "%s", "n8n_workflows": %s, "n8n_credentials": %s },\n' \
    "$app_rows" "$app_digest" "${wf_count_db:-0}" "$cred_rows"
  printf '  "artifacts": [\n'
} > "$manifest"

first=1
artifact_list="$(mktemp)"
find "$BK" -type f ! -name manifest.json | sort > "$artifact_list"
echo "$KEYDIR/n8n_encryption_key.txt" >> "$artifact_list"
while IFS= read -r f; do
  if [ "$first" -eq 0 ]; then
    printf ',\n' >> "$manifest"
  fi
  first=0
  printf '    { "path": "%s", "bytes": %s, "sha256": "%s" }' \
    "$f" "$(stat -c %s "$f")" "$(sha256sum "$f" | cut -d' ' -f1)" >> "$manifest"
done < "$artifact_list"
rm -f "$artifact_list"
printf '\n  ]\n}\n' >> "$manifest"
cat "$manifest"

echo "backup_total_seconds=$(secs "$total_start")"
echo "backup_dir=${BK}"
