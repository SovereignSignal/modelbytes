#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${MODELBYTES_BACKUP_DIR:-/var/backups/modelbytes}"
RETENTION_DAYS="${MODELBYTES_BACKUP_RETENTION_DAYS:-14}"
COMPOSE_FILE="${MODELBYTES_COMPOSE_FILE:-/opt/modelbytes/deploy/vm/docker-compose.yml}"
POSTGRES_USER="${POSTGRES_USER:-modelbytes}"
POSTGRES_DB="${POSTGRES_DB:-modelbytes}"

mkdir -p "$BACKUP_DIR"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
tmp_path="$BACKUP_DIR/modelbytes-$stamp.sql.gz.tmp"
final_path="$BACKUP_DIR/modelbytes-$stamp.sql.gz"
trap 'rm -f "$tmp_path"' EXIT

docker compose -f "$COMPOSE_FILE" exec -T postgres \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip -9 > "$tmp_path"

mv "$tmp_path" "$final_path"
trap - EXIT
find "$BACKUP_DIR" -type f -name 'modelbytes-*.sql.gz' -mtime +"$RETENTION_DAYS" -delete

echo "Wrote $final_path"
