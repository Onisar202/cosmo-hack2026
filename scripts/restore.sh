#!/usr/bin/env bash
# FN-45: восстановление постоянного тома api-data из бэкапа scripts/backup.sh.
# ДЕСТРУКТИВНО по намерению (заменяет текущие данные архивом) но не по
# отказу: архив проверяется в отдельном временном томе до того, как рабочий
# том вообще трогается, прежние данные снимаются в свой временный том перед
# заменой, а EXIT-trap отдельно отслеживает три независимых факта —
# "сервисы были остановлены этим скриптом" (флаг ставится ДО compose stop,
# переживает его частичный отказ), "рабочий том уже тронут" и "безопасно ли
# вообще поднимать сервисы" — и на любом сбое до подтверждённого
# health-check: останавливает сервисы (если ещё не остановлены) ПЕРЕД тем как
# трогать том, возвращает снимок через rm && cp (не `;`) с проверкой
# результата, поднимает сервисы обратно ТОЛЬКО если откат подтверждённо удался
# (или том не трогался вовсе) и ждёт их собственный health-check — сервисы,
# оставшиеся без здорового ответа, остаются остановленными, а не поднятыми
# вслепую поверх сомнительных данных. rollback-том удаляется только когда весь
# путь возврата (откат + health-check) подтверждён; при любой неудаче том
# сохраняется, а его имя печатается для ручного восстановления
# (round 1 + round 2 + round 3 + round 4 ревью PR #36).
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

# Три независимых флага для trap ниже (round 3 ревью PR #36: одного
# "restore_touched_volume" было недостаточно — сервисы могли быть остановлены
# ДО того, как том вообще тронут, и trap обязан вернуть их независимо от
# того, дошло ли дело до замены данных):
#   services_stopped   — этот скрипт остановил api/web (значит обязан их
#                         поднять обратно на любом выходе, что бы ни случилось);
#   volume_touched      — рабочий том уже заменяется/заменён (снимок нужно
#                         вернуть, а не просто поднять сервисы со старыми данными);
#   restore_confirmed   — health-check после восстановления прошёл; ДО этого
#                         момента любой выход считается неудачей.
services_stopped=0
volume_touched=0
restore_confirmed=0
cleanup_and_revert() {
    local exit_code=$?
    local revert_ok=1
    # safe_to_start: разрешение trap'у поднимать сервисы вообще. Остаётся 1,
    # если том не трогался (нечего портить) или откат снимка подтверждённо
    # удался; становится 0, если откат провалился — данные в томе в этом
    # случае могут быть частично очищены/повреждены, и trap не должен
    # запускать api поверх них вслепую (round 4 ревью PR #36).
    local safe_to_start=1

    if [[ "$restore_confirmed" -ne 1 ]]; then
        if [[ "$volume_touched" -eq 1 ]]; then
            log "восстановление не подтверждено — возвращаю предыдущие данные из снимка (trap)"
            # Сервисы обязаны быть остановлены ПЕРЕД тем как trap трогает том —
            # если health-check уронил set -e уже после `compose up -d`,
            # контейнеры на этот момент запущены и пишут в тот же том.
            compose stop api web >/dev/null 2>&1 || true
            # rm и cp связаны && (не ;), как и в основном пути ниже — иначе
            # неудачная очистка не остановит попытку копирования поверх
            # недочищенного тома. Результат проверяется, а не глушится
            # безусловным `|| true` (round 3 ревью PR #36).
            if docker run --rm \
                    -v "${VOLUME_NAME}:/data" \
                    -v "${rollback_volume}:/rollback:ro" \
                    alpine:3.20 \
                    sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null && cp -a /rollback/. /data/' \
                && docker run --rm -v "${VOLUME_NAME}:/data" alpine:3.20 test -f /data/store.sqlite3
            then
                log "ok: снимок успешно возвращён в том $VOLUME_NAME"
            else
                revert_ok=0
                safe_to_start=0
                log "ОШИБКА: автоматический откат не удался! Том $VOLUME_NAME может быть частично очищен/повреждён — сервисы НЕ запускаются. rollback-том СОХРАНЁН (не удаляется): $rollback_volume"
                log "Восстановите вручную, затем поднимите сервисы: docker run --rm -v ${VOLUME_NAME}:/data -v ${rollback_volume}:/rollback:ro alpine:3.20 sh -c 'rm -rf /data/* /data/.[!.]*; cp -a /rollback/. /data/' && docker compose up -d api web"
            fi
        fi

        if [[ "$services_stopped" -eq 1 ]]; then
            if [[ "$safe_to_start" -eq 1 ]]; then
                # Даже успешный откат не значит "сервис работает" — поднимаем
                # и обязательно ждём health-check, а не считаем trap успешным
                # по одному лишь `compose up -d` (round 4 ревью PR #36).
                if compose up -d api web >/dev/null 2>&1 && "$SCRIPT_DIR/wait-healthy.sh" 60; then
                    log "ok: сервисы подняты и прошли health-check (trap)"
                else
                    revert_ok=0
                    # Восстановление не подтверждено — сервисы не должны
                    # остаться запущенными (частичный `compose up`/неудачный
                    # health-check иначе оставляет контейнеры работать над
                    # непроверенными данными). Явно останавливаем, а не просто
                    # логируем ошибку (round 5 ревью PR #36).
                    log "ОШИБКА: сервисы не поднялись или не прошли health-check после отката — останавливаю их"
                    if compose stop api web >/dev/null 2>&1; then
                        log "сервисы остановлены"
                    else
                        log "ОШИБКА: не удалось остановить сервисы — проверьте вручную (docker compose ps/logs)"
                    fi
                    log "rollback-том СОХРАНЁН для ручного разбора: $rollback_volume"
                fi
            else
                log "сервисы оставлены остановленными — том в подозрительном состоянии, см. инструкцию выше"
            fi
        fi
    fi

    docker volume rm -f "$staging_volume" >/dev/null 2>&1 || true
    if [[ "$revert_ok" -eq 1 ]]; then
        docker volume rm -f "$rollback_volume" >/dev/null 2>&1 || true
    fi
    exit "$exit_code"
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
    # Флаг ставится ДО compose stop, не после: частичный отказ (например,
    # остановлен api, но не web) всё равно прервёт скрипт через set -e — trap
    # обязан попытаться привести сервисы в рабочее состояние (`compose up -d`
    # идемпотентен) независимо от кода возврата самого stop (round 4 ревью
    # PR #36).
    services_stopped=1
    compose stop api web
fi

# Снимок ПОСЛЕ остановки сервисов — консистентный (тот же принцип, что и
# backup.sh: копирование "на лету" под записью рискует захватить SQLite в
# промежуточном состоянии). Если этот шаг упадёт, volume_touched всё ещё 0
# (рабочий том не тронут) — trap просто поднимет остановленные сервисы
# обратно (services_stopped=1), не пытаясь ничего откатывать.
log "сохраняю снимок текущих данных тома $VOLUME_NAME (на случай отката)"
docker run --rm \
    -v "${VOLUME_NAME}:/data:ro" \
    -v "${rollback_volume}:/rollback" \
    alpine:3.20 \
    sh -c 'cp -a /data/. /rollback/'

log "архив проверен — заменяю содержимое тома $VOLUME_NAME"
volume_touched=1
# rm и cp связаны && (не ;): если очистка не завершится успешно, копирование
# не запустится поверх недочищенного тома, и следующая же команда всё равно
# провалится через set -e — сработает trap-откат.
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
restore_confirmed=1

log "восстановление завершено из $archive"
