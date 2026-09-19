#!/usr/bin/env bash
# FN-45: обновление production-стека до нового кода без потери данных в
# api-data (SQLite + raw originals — именованный том, пересборка образов
# его не трогает).
#
#   scripts/upgrade.sh [git-ref]     # по умолчанию: origin/<текущая ветка>
#
# Печатает git-коммит ДО обновления — использовать его как аргумент
# scripts/rollback.sh, если после обновления smoke-проверка не проходит.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
cd "$(repo_root)"

require_cmd git
[[ -d .git ]] || die "upgrade.sh рассчитан на развёртывание из git-чекаута (найти/переключить ref), а не на образ без .git."

[[ -z "$(git status --porcelain)" ]] || die "в рабочем дереве есть незакоммиченные изменения — upgrade.sh переключает git ref и не должен их потерять. Закоммитьте/уберите их (git stash) и повторите."

current_branch="$(git rev-parse --abbrev-ref HEAD)"
target_ref="${1:-origin/${current_branch}}"
previous_commit="$(git rev-parse HEAD)"

log "текущий коммит: $previous_commit (запомните для scripts/rollback.sh $previous_commit при неудаче)"
log "git fetch"
git fetch origin --quiet

log "переключение на $target_ref"
git checkout --quiet "$target_ref"
new_commit="$(git rev-parse HEAD)"
[[ "$new_commit" == "$previous_commit" ]] && log "  без изменений ($new_commit) — пересборка и перезапуск всё равно выполняются."

"$SCRIPT_DIR/preflight.sh"

log "docker compose build"
compose build

log "docker compose up -d"
compose up -d

"$SCRIPT_DIR/wait-healthy.sh"

log "smoke-проверка основного пути"
if ! "$SCRIPT_DIR/smoke.sh"; then
    die "smoke-проверка провалилась после обновления до $new_commit. Откат: scripts/rollback.sh $previous_commit"
fi

log "upgrade завершён: $previous_commit -> $new_commit"
