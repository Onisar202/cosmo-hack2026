#!/usr/bin/env bash
# FN-45: восстановление постоянного тома api-data из бэкапа scripts/backup.sh.
# ДЕСТРУКТИВНО: полностью заменяет текущее содержимое тома (SQLite +
# raw originals) содержимым архива — требует явного подтверждения.
#
#   scripts/restore.sh <архив.tar.gz> [--yes]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
cd "$(repo_root)"

require_cmd docker
require_cmd jq

archive="${1:-}"
[[ -n "$archive" ]] || die "нужен путь к архиву: scripts/restore.sh <архив.tar.gz> (создаётся scripts/backup.sh)."
[[ -f "$archive" ]] || die "архив не найден: $archive"
archive="$(cd "$(dirname "$archive")" && pwd)/$(basename "$archive")"

auto_yes="${2:-}"
if [[ "$auto_yes" != "--yes" ]]; then
    printf 'ВНИМАНИЕ: это ПОЛНОСТЬЮ заменит текущие данные api (SQLite + raw originals) содержимым %s.\nПродолжить? Введите "yes": ' "$archive" >&2
    read -r confirm
    [[ "$confirm" == "yes" ]] || die "отменено пользователем."
fi

api_container="$(compose ps -q api)"
if [[ -n "$api_container" ]]; then
    VOLUME_NAME="$(docker inspect --format '{{ range .Mounts }}{{ if eq .Destination "/app/data" }}{{ .Name }}{{ end }}{{ end }}' "$api_container")"
    log "останавливаю api и web (восстановление тома под работающим сервисом небезопасно)"
    compose stop api web
else
    VOLUME_NAME="$(docker compose config --format json | jq -r '.volumes["api-data"].name')"
fi
[[ -n "${VOLUME_NAME:-}" && "$VOLUME_NAME" != "null" ]] || die "не удалось определить имя тома api-data."

log "очищаю том $VOLUME_NAME и распаковываю $archive"
docker run --rm \
    -v "${VOLUME_NAME}:/data" \
    -v "$(dirname "$archive"):/backup:ro" \
    alpine:3.20 \
    sh -c "rm -rf /data/* /data/.[!.]* 2>/dev/null; tar xzf /backup/$(basename "$archive") -C /data"

log "запускаю api и web"
compose up -d api web
"$SCRIPT_DIR/wait-healthy.sh"

log "восстановление завершено из $archive"
