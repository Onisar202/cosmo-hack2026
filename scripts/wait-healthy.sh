#!/usr/bin/env bash
# FN-45: ждёт, пока оба сервиса compose.yaml (api, web) станут `healthy` по
# их HEALTHCHECK (Dockerfile/compose.yaml), с понятной ошибкой при таймауте
# вместо тихого зависания. Используется scripts/deploy.sh, upgrade.sh,
# rollback.sh; можно запускать отдельно после ручного `docker compose up`.
#
# Использование: scripts/wait-healthy.sh [таймаут_секунд=120]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
cd "$(repo_root)"

TIMEOUT_SECONDS="${1:-120}"
SERVICES=(api web)

deadline=$((SECONDS + TIMEOUT_SECONDS))
for service in "${SERVICES[@]}"; do
    log "ожидание healthy: $service (таймаут ${TIMEOUT_SECONDS}s)"
    while true; do
        container_id="$(compose ps -q "$service")"
        if [[ -z "$container_id" ]]; then
            status="not-created"
        else
            status="$(docker inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || echo "unknown")"
        fi
        [[ "$status" == "healthy" ]] && { log "  $service: healthy"; break; }
        if (( SECONDS >= deadline )); then
            die "$service не стал healthy за ${TIMEOUT_SECONDS}s (последний статус: $status). Логи: docker compose logs $service"
        fi
        sleep 2
    done
done
