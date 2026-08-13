#!/bin/sh
set -eu

umask 077
backup_dir="${1:-./backups}"
db_user="${POSTGRES_USER:-bilibili}"
db_name="${POSTGRES_DB:-bilibili_charge_hub}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${backup_dir}/${db_name}-${timestamp}.dump"
partial="${target}.partial"

mkdir -p "$backup_dir"
trap 'rm -f "$partial"' EXIT HUP INT TERM

docker compose exec -T db pg_dump \
  --username "$db_user" \
  --format custom \
  --no-owner \
  --no-acl \
  "$db_name" > "$partial"

test -s "$partial"
docker compose exec -T db pg_restore --list < "$partial" > "${target}.manifest"
mv "$partial" "$target"

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$target" > "${target}.sha256"
else
  shasum -a 256 "$target" > "${target}.sha256"
fi

printf 'Backup created: %s\n' "$target"
printf 'Store this dump, its checksum, and CREDENTIAL_ENCRYPTION_KEY separately.\n'
