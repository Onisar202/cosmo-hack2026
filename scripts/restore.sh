#!/usr/bin/env bash
# FN-45: восстановление постоянного тома api-data из бэкапа scripts/backup.sh.
# ДЕСТРУКТИВНО по намерению (заменяет текущие данные архивом) но не по
# отказу: архив проверяется в отдельном временном томе до того, как рабочий
# том вообще трогается, а прежние данные снимаются в свой временный том
# перед заменой — если что-то пойдёт не так (сбой копирования, не запустился
# healthy api), EXIT-trap возвращает снятый снимок и поднимает сервисы, а не
# оставляет том частично заполненным и сервисы остановленными
# (round 1 + round 2 ревью PR #36).
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
rollback_volume="fn45-restore-rollback-$$"
docker volume create "$staging_volume" >/dev/null
docker volume create "$rollback_volume" >/dev/null

# restore_touched_volume: рабочий том api-data ещё не менялся (пока только
# staging/rollback тома существуют) — реагировать на сбой откатом нечего.
# restore_failed: сбрасывается в 0 только после успешного health-check в
# самом конце — до этого момента ЛЮБОЙ выход из скрипта (set -e на любой
# команде ниже) считается неудачей. Один trap отвечает и за откат (только
# если оба флага это требуют), и за очистку временных томов на любом выходе
# (round 2 ревью PR #36: явное `trap - EXIT` снималось раньше, чем
# подтверждался health-check, и оставляло частично применённое восстановление
# без отката).
restore_touched_volume=0
restore_failed=1
cleanup_and_revert() {
    if [[ "$restore_touched_volume" -eq 1 && "$restore_failed" -eq 1 ]]; then
        log "восстановление не подтверждено health-check'ом — возвращаю предыдущие данные из снимка (trap)"
        docker run --rm \
            -v "${VOLUME_NAME}:/data" \
            -v "${rollback_volume}:/rollback:ro" \
            alpine:3.20 \
            sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null; cp -a /rollback/. /data/' >/dev/null 2>&1 || true
        compose up -d api web >/dev/null 2>&1 || true
    fi
    docker volume rm -f "$staging_volume" "$rollback_volume" >/dev/null 2>&1 || true
}
trap cleanup_and_revert EXIT

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

# Снимок ПОСЛЕ остановки сервисов — консистентный (тот же принцип, что и
# backup.sh: копирование "на лету" под записью рискует захватить SQLite в
# промежуточном состоянии), и именно он возвращается trap'ом при сбое ниже.
log "сохраняю снимок текущих данных тома $VOLUME_NAME (на случай отката)"
docker run --rm \
    -v "${VOLUME_NAME}:/data:ro" \
    -v "${rollback_volume}:/rollback" \
    alpine:3.20 \
    sh -c 'cp -a /data/. /rollback/'

log "архив проверен — заменяю содержимое тома $VOLUME_NAME"
restore_touched_volume=1
# rm и cp связаны && (не ;): если очистка не завершится успешно, копирование
# не запустится поверх недочищенного тома, и следующая же команда всё равно
# провалится через set -e — сработает trap-откат (round 2 ревью PR #36).
docker run --rm \
    -v "${VOLUME_NAME}:/data" \
    -v "${staging_volume}:/staging:ro" \
    alpine:3.20 \
    sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null && cp -a /staging/. /data/'

docker run --rm -v "${VOLUME_NAME}:/data" alpine:3.20 \
    test -f /data/store.sqlite3 \
    || die "после копирования store.sqlite3 не найден в целевом томе — восстановление повреждено."
log "ok: восстановленный том содержит store.sqlite3"

log "запускаю api и web"
compose up -d api web
"$SCRIPT_DIR/wait-healthy.sh"

# Только теперь восстановление подтверждено — trap на выходе script'а не
# станет откатывать снимок (но всё ещё уберёт staging/rollback тома).
restore_failed=0

log "восстановление завершено из $archive"
