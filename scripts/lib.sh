#!/usr/bin/env bash
# Общие хелперы для scripts/*.sh (FN-45: deploy/upgrade/rollback/backup/
# restore/smoke/preflight). Источник, не самостоятельный скрипт — `source
# "$(dirname "${BASH_SOURCE[0]}")/lib.sh"`.

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() {
    printf '[%s] ОШИБКА: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "требуется '$1', но он не найден в PATH."
}

repo_root() {
    cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

# Имя compose-проекта и путь к compose.yaml берутся из текущей директории —
# все скрипты запускаются из корня репозитория (см. `repo_root`).
compose() {
    docker compose "$@"
}
