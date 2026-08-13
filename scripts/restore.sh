#!/bin/sh
set -eu

if [ "$#" -ne 2 ] || [ "$2" != "--confirm-destructive-restore" ]; then
  printf 'Usage: %s BACKUP.dump --confirm-destructive-restore\n' "$0" >&2
  exit 2
fi

backup="$1"
db_user="${POSTGRES_USER:-bilibili}"
db_name="${POSTGRES_DB:-bilibili_charge_hub}"
validation_db="${db_name}_restore_check_$$"
app_stopped=0

test -s "$backup"
docker compose exec -T db pg_restore --list < "$backup" >/dev/null

if [ -f "${backup}.sha256" ]; then
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check "${backup}.sha256"
  else
    expected="$(cut -d ' ' -f 1 "${backup}.sha256")"
    actual="$(shasum -a 256 "$backup" | cut -d ' ' -f 1)"
    test "$expected" = "$actual"
  fi
fi

cleanup_validation() {
  docker compose exec -T db dropdb --username "$db_user" --if-exists "$validation_db" \
    >/dev/null 2>&1 || true
}
cleanup() {
  cleanup_validation
  if [ "$app_stopped" -eq 1 ]; then
    docker compose start app >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT HUP INT TERM

cleanup_validation
docker compose exec -T db createdb --username "$db_user" "$validation_db"
docker compose exec -T db pg_restore \
  --username "$db_user" \
  --dbname "$validation_db" \
  --no-owner \
  --no-acl \
  --exit-on-error < "$backup"
docker compose exec -T db psql \
  --username "$db_user" \
  --dbname "$validation_db" \
  --tuples-only \
  --command "SELECT version_num FROM alembic_version;" >/dev/null
docker compose exec -T db psql \
  --username "$db_user" \
  --dbname "$validation_db" \
  --tuples-only \
  --command "SELECT count(*) FROM users;" >/dev/null
cleanup_validation

docker compose stop app
app_stopped=1
docker compose exec -T db pg_restore \
  --username "$db_user" \
  --dbname "$db_name" \
  --clean \
  --if-exists \
  --no-owner \
  --no-acl \
  --exit-on-error < "$backup"
docker compose start app
app_stopped=0

printf 'Restore completed. Verify /readyz, user login, account count, and credential decryption.\n'
