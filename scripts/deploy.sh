#!/usr/bin/env bash
# FN-45: первый запуск production-стека на чистом хосте.
#   scripts/deploy.sh
# Идемпотентен: повторный запуск пересобирает изменившиеся слои и не трогает
# существующий том api-data (постоянство результатов, README.md
# «Постоянство: перезапуск не теряет результат»).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
cd "$(repo_root)"

"$SCRIPT_DIR/preflight.sh"

log "docker compose build"
compose build

log "docker compose up -d"
compose up -d

"$SCRIPT_DIR/wait-healthy.sh"

api_port="${API_PORT:-8000}"
web_port="${WEB_PORT:-8080}"
log "готово: UI http://localhost:${web_port}  API http://localhost:${api_port}"
log "проверка основного пути: scripts/smoke.sh"
