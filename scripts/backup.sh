#!/usr/bin/env bash
# FN-45: резервная копия постоянного тома api-data (SQLite store.sqlite3 +
# raw originals, compose.yaml) в один tar.gz на хосте.
#
#   scripts/backup.sh [каталог=./backups]
#
# Останавливает контейнер api на время копирования (несколько секунд) для
# консистентного снимка SQLite-файла — копирование "на лету" под записью
# рискует захватить файл в промежуточном состоянии между обновлением
# страницы и журналом. web не останавливается (не пишет в том).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
cd "$(repo_root)"

require_cmd docker
require_cmd jq
BACKUP_DIR="${1:-backups}"
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"

api_container="$(compose ps -q api)"
if [[ -n "$api_container" ]]; then
    VOLUME_NAME="$(docker inspect --format '{{ range .Mounts }}{{ if eq .Destination "/app/data" }}{{ .Name }}{{ end }}{{ end }}' "$api_container")"
else
    # api не запущен — определяем имя тома, которое использует ЭТОТ
    # compose-проект (зависит от каталога/COMPOSE_PROJECT_NAME), не гадаем.
    VOLUME_NAME="$(docker compose config --format json | jq -r '.volumes["api-data"].name')"
fi
[[ -n "$VOLUME_NAME" && "$VOLUME_NAME" != "null" ]] || die "не удалось определить имя тома api-data — запустите 'docker compose up -d' хотя бы раз или проверьте COMPOSE_PROJECT_NAME."

if [[ -n "$api_container" ]]; then
    log "останавливаю api для консистентного снимка (том $VOLUME_NAME)"
    compose stop api
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$BACKUP_DIR/api-data-${timestamp}.tar.gz"

log "архивирую том $VOLUME_NAME -> $archive"
docker run --rm \
    -v "${VOLUME_NAME}:/data:ro" \
    -v "${BACKUP_DIR}:/backup" \
    alpine:3.20 \
    tar czf "/backup/$(basename "$archive")" -C /data .

if [[ -n "$api_container" ]]; then
    log "перезапускаю api"
    compose start api
    "$SCRIPT_DIR/wait-healthy.sh" 60
fi

log "готово: $archive ($(du -h "$archive" | cut -f1))"
