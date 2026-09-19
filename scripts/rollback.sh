#!/usr/bin/env bash
# FN-45: откат production-стека на известный рабочий git-ref (коммит/тег),
# без потери данных в api-data (том не трогается пересборкой образов).
#
#   scripts/rollback.sh <git-ref>    # ref обязателен — без него нет
#                                     # объективно верного выбора "на что откатывать"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
cd "$(repo_root)"

target_ref="${1:-}"
[[ -n "$target_ref" ]] || die "нужен git-ref для отката: scripts/rollback.sh <коммит-или-тег>. Текущий коммит: $(git rev-parse HEAD 2>/dev/null || echo неизвестен)."

require_cmd git
[[ -d .git ]] || die "rollback.sh рассчитан на развёртывание из git-чекаута."
[[ -z "$(git status --porcelain)" ]] || die "в рабочем дереве есть незакоммиченные изменения — откат их потеряет. Закоммитьте/уберите их (git stash) и повторите."
git rev-parse --verify --quiet "${target_ref}^{commit}" >/dev/null || die "'$target_ref' не найден локально — сначала 'git fetch origin'."

previous_commit="$(git rev-parse HEAD)"
log "откат: $previous_commit -> $target_ref"
git checkout --quiet "$target_ref"

"$SCRIPT_DIR/preflight.sh"

log "docker compose build"
compose build

log "docker compose up -d"
compose up -d

"$SCRIPT_DIR/wait-healthy.sh"

log "smoke-проверка основного пути"
if ! "$SCRIPT_DIR/smoke.sh"; then
    die "smoke-проверка провалилась и после отката на $target_ref — нужна ручная диагностика (docker compose logs)."
fi

log "rollback завершён: $previous_commit -> $(git rev-parse HEAD)"
