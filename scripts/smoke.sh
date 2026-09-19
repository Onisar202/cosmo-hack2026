#!/usr/bin/env bash
# FN-45: production smoke checklist против уже поднятого стека (docker
# compose локально, или произвольный публичный URL) — без uv/pytest,
# только curl+jq, чтобы проверку мог выполнить кто угодно на площадке
# развёртывания. Дополняет (не заменяет) tests/integration/test_stage1.py
# (README.md «Сквозная проверка этапа 1») тем же путём, но зависимостями
# только для production-хоста и добавляет проверку JSON/HTML export.
#
# Проверяет: health -> создание расчёта -> опрос статуса -> результат ->
#            JSON export -> HTML export -> restart api -> результат уцелел.
#
# Переменные окружения:
#   SMOKE_BASE_URL     базовый URL (по умолчанию http://localhost:${WEB_PORT:-8080},
#                       т. е. через реверс-прокси nginx — тот же путь, что и у
#                       браузера, а не в обход него)
#   SMOKE_COMPOSE_CMD   команда перезапуска api для шага persistence
#                       (по умолчанию "docker compose"; пусто — пропустить
#                       шаг restart, например при проверке публичного URL,
#                       где нет доступа к docker compose этого хоста)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
cd "$(repo_root)"

require_cmd curl
require_cmd jq

BASE_URL="${SMOKE_BASE_URL:-http://localhost:${WEB_PORT:-8080}}"
BASE_URL="${BASE_URL%/}"
COMPOSE_CMD="${SMOKE_COMPOSE_CMD-docker compose}"
POLL_TIMEOUT_SECONDS=90
POLL_INTERVAL_SECONDS=2
# main-prompt.md §11: отказ источника — это НЕ дефект развёртывания, если
# код входит в этот известный список (тот же список, что и
# tests/integration/test_stage1.py: SOURCE_UNAVAILABLE_ERROR_CODES).
SOURCE_UNAVAILABLE_CODES=("orbit_error_source" "orbit_error_quota")

step() { printf '\n== %s ==\n' "$*" >&2; }

step "1/7 health ($BASE_URL/health)"
health_body="$(curl -fsS "$BASE_URL/health")"
[[ "$(jq -r '.status' <<<"$health_body")" == "ok" ]] || die "health вернул не ok: $health_body"
log "ok: service=$(jq -r '.service' <<<"$health_body")"

step "2/7 создание расчёта (mode=current)"
start_at="$(date -u -d '+1 minute' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+1M +%Y-%m-%dT%H:%M:%SZ)"
payload="$(jq -n --arg start_at "$start_at" '{mode: "current", start_at: $start_at, duration_hours: 4, search_window_hours: 8}')"
create_response="$(curl -fsS -X POST "$BASE_URL/api/calculations" -H 'Content-Type: application/json' -d "$payload")"
task_id="$(jq -r '.task_id' <<<"$create_response")"
[[ -n "$task_id" && "$task_id" != "null" ]] || die "нет task_id в ответе: $create_response"
log "task_id=$task_id"

step "3/7 опрос статуса задания"
deadline=$((SECONDS + POLL_TIMEOUT_SECONDS))
job_status=""
job_body=""
while true; do
    job_body="$(curl -fsS "$BASE_URL/api/calculations/$task_id")"
    job_status="$(jq -r '.status' <<<"$job_body")"
    [[ "$job_status" == "done" || "$job_status" == "failed" ]] && break
    (( SECONDS >= deadline )) && die "задание $task_id не завершилось за ${POLL_TIMEOUT_SECONDS}s: $job_body"
    sleep "$POLL_INTERVAL_SECONDS"
done
log "status=$job_status"

if [[ "$job_status" == "failed" ]]; then
    error_code="$(jq -r '.error.code // empty' <<<"$job_body")"
    known=0
    for code in "${SOURCE_UNAVAILABLE_CODES[@]}"; do
        [[ "$code" == "$error_code" ]] && known=1
    done
    if [[ "$known" -eq 0 ]]; then
        die "задание завершилось ошибкой с кодом вне известного списка отказов источника ${SOURCE_UNAVAILABLE_CODES[*]} — похоже на дефект развёртывания/сервиса: $job_body"
    fi
    log "ПРЕДУПРЕЖДЕНИЕ: реальный источник (CelesTrak/NOAA SWPC) недоступен из этой сети (code=$error_code) — это не сбой развёртывания (README.md «Известное ограничение окружения сборки»), но полный путь (result/export/restart) этим прогоном не проверен."
    printf '\nSMOKE PARTIAL: health и создание/опрос задания пройдены; источник данных недоступен из этой сети — result/export/restart не проверены.\n'
    exit 0
fi

result_id="$(jq -r '.result_id' <<<"$job_body")"
[[ -n "$result_id" && "$result_id" != "null" ]] || die "status=done, но нет result_id: $job_body"
log "result_id=$result_id"

step "4/7 получение результата"
result_body="$(curl -fsS "$BASE_URL/api/results/$result_id")"
[[ "$(jq -r '.result_id' <<<"$result_body")" == "$result_id" ]] || die "GET /api/results/$result_id вернул другой result_id: $result_body"

step "5/7 JSON export"
json_export="$(curl -fsS "$BASE_URL/api/results/$result_id/export.json")"
[[ "$(jq -r '.result_id' <<<"$json_export")" == "$result_id" ]] || die "export.json: result_id не совпадает"

step "6/7 HTML export"
html_export="$(curl -fsS "$BASE_URL/api/results/$result_id/export.html")"
grep -qF "$result_id" <<<"$html_export" || die "export.html не содержит result_id ($result_id) — выгрузка разошлась с интерфейсом (main-prompt.md §3)"

step "7/7 restart api -> результат сохраняется"
restart_verified=0
if [[ -z "$COMPOSE_CMD" ]]; then
    log "SMOKE_COMPOSE_CMD пуст — шаг restart пропущен (например, проверка публичного URL без доступа к docker compose этого хоста)"
else
    if ! $COMPOSE_CMD restart api >/dev/null 2>&1; then
        log "не удалось выполнить '$COMPOSE_CMD restart api' — похоже, сервис запущен не через docker compose на этом хосте; шаг пропущен"
    else
        "$SCRIPT_DIR/wait-healthy.sh" 60
        after_restart="$(curl -fsS "$BASE_URL/api/results/$result_id")"
        [[ "$after_restart" == "$result_body" ]] || die "результат $result_id изменился после restart api — постоянство тома нарушено"
        log "ok: результат $result_id побайтово идентичен после restart"
        restart_verified=1
    fi
fi

printf '\nSMOKE PASSED: health, create, poll, result, JSON export, HTML export%s\n' \
    "$([[ "$restart_verified" -eq 1 ]] && echo ", restart persistence" || echo " (restart не проверен — пусто/недоступен docker compose на этом хосте)")"
