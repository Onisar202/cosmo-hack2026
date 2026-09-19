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

# target_ref может не содержать scripts/ вовсе (откат на коммит до FN-45) —
# `git checkout` меняет рабочее дерево целиком и вырубил бы этот же
# запущенный скрипт и preflight/wait-healthy/smoke из-под себя. Копируем
# ТЕКУЩИЕ scripts/ во временный каталог до переключения и используем эту
# копию для всех шагов ниже; FN45_REPO_ROOT (lib.sh:repo_root) указывает ей
# на реальный репозиторий, а не на temp-копию (round 1 ревью PR #36).
runner_dir="$(mktemp -d "${TMPDIR:-/tmp}/fn45-rollback-runner-XXXXXX")"
cleanup_runner() { rm -rf "$runner_dir"; }
trap cleanup_runner EXIT
cp -a "$SCRIPT_DIR/." "$runner_dir/"
FN45_REPO_ROOT="$(repo_root)"
export FN45_REPO_ROOT

previous_commit="$(git rev-parse HEAD)"
log "откат: $previous_commit -> $target_ref"
git checkout --quiet "$target_ref"

"$runner_dir/preflight.sh"

log "docker compose build"
compose build

log "docker compose up -d"
compose up -d

"$runner_dir/wait-healthy.sh"

log "smoke-проверка основного пути"
if ! "$runner_dir/smoke.sh"; then
    die "smoke-проверка провалилась и после отката на $target_ref — нужна ручная диагностика (docker compose logs)."
fi

log "rollback завершён: $previous_commit -> $(git rev-parse HEAD)"
