#!/usr/bin/env bash
# FN-45: восстановление постоянного тома api-data из бэкапа scripts/backup.sh.
# ДЕСТРУКТИВНО: полностью заменяет текущее содержимое тома (SQLite +
# raw originals) содержимым архива — требует явного подтверждения.
#
#   scripts/restore.sh <архив.tar.gz> [--yes]
#
# Архив сначала проверяется (валидный tar.gz, содержит store.sqlite3) в
# отдельном временном томе — том api-data очищается, только если проверка
# прошла: повреждённый/неполный архив не должен оставить хранилище пустым
# или частично восстановленным (round 1 ревью PR #36).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
cd "$(repo_root)"

require_cmd docker
require_cmd jq

archive="${1:-}"
[[ -n "$archive" ]] || die "нужен путь к архиву: scripts/restore.sh <архив.tar.gz> (создаётся scripts/backup.sh)."
[[ -f "$archive" ]] || die "архив не найден: $archive"
archive_dir="$(cd "$(dirname "$archive")" && pwd)"
archive_name="$(basename "$archive")"
archive="$archive_dir/$archive_name"

auto_yes="${2:-}"
if [[ "$auto_yes" != "--yes" ]]; then
    printf 'ВНИМАНИЕ: это ПОЛНОСТЬЮ заменит текущие данные api (SQLite + raw originals) содержимым %s.\nПродолжить? Введите "yes": ' "$archive" >&2
    read -r confirm
    [[ "$confirm" == "yes" ]] || die "отменено пользователем."
fi

api_container="$(compose ps -q api)"
if [[ -n "$api_container" ]]; then
    VOLUME_NAME="$(docker inspect --format '{{ range .Mounts }}{{ if eq .Destination "/app/data" }}{{ .Name }}{{ end }}{{ end }}' "$api_container")"
else
    VOLUME_NAME="$(docker compose config --format json | jq -r '.volumes["api-data"].name')"
fi
[[ -n "${VOLUME_NAME:-}" && "$VOLUME_NAME" != "null" ]] || die "не удалось определить имя тома api-data."

# Имя архива передаётся позиционным аргументом sh -c ($1), а не
# интерполируется в текст команды — иначе имя файла с спецсимволами
# оболочки (пробелы, кавычки, `$()`) исказило бы или подменило команду
# внутри контейнера (round 1 ревью PR #36).
log "проверяю архив: $archive"
docker run --rm \
    -v "${archive_dir}:/backup:ro" \
    alpine:3.20 \
    sh -c 'tar tzf "/backup/$1" >/dev/null' _ "$archive_name" \
    || die "архив повреждён или не является tar.gz: $archive"

staging_volume="fn45-restore-staging-$$"
docker volume create "$staging_volume" >/dev/null
cleanup_staging() { docker volume rm -f "$staging_volume" >/dev/null 2>&1 || true; }
trap cleanup_staging EXIT

log "распаковываю архив во временный том (текущие данные пока не тронуты)"
docker run --rm \
    -v "${staging_volume}:/staging" \
    -v "${archive_dir}:/backup:ro" \
    alpine:3.20 \
    sh -c 'tar xzf "/backup/$1" -C /staging' _ "$archive_name"

docker run --rm -v "${staging_volume}:/staging" alpine:3.20 \
    test -f /staging/store.sqlite3 \
    || die "архив не содержит store.sqlite3 — не похоже на бэкап api-data (scripts/backup.sh). Восстановление отменено, текущие данные НЕ тронуты."
log "ok: архив содержит ожидаемое store.sqlite3"

api_container="$(compose ps -q api)"
if [[ -n "$api_container" ]]; then
    log "останавливаю api и web (восстановление тома под работающим сервисом небезопасно)"
    compose stop api web
fi

log "архив проверен — заменяю содержимое тома $VOLUME_NAME"
docker run --rm \
    -v "${VOLUME_NAME}:/data" \
    -v "${staging_volume}:/staging:ro" \
    alpine:3.20 \
    sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null; cp -a /staging/. /data/'

log "запускаю api и web"
compose up -d api web
"$SCRIPT_DIR/wait-healthy.sh"

log "восстановление завершено из $archive"
