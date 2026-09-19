#!/usr/bin/env bash
# FN-45: preflight — проверяет чистое окружение ДО `docker compose up`,
# чтобы отказ был понятным сообщением здесь, а не непрозрачной ошибкой
# контейнера в середине запуска. Ничего не запускает и не меняет.
#
# Использование: scripts/preflight.sh (без аргументов, идемпотентен).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

cd "$(repo_root)"

log "1/5 инструменты: docker, docker compose"
require_cmd docker
docker info >/dev/null 2>&1 || die "docker demon недоступен (проверьте, что Docker Desktop/dockerd запущен и текущий пользователь имеет доступ к сокету)."
docker compose version >/dev/null 2>&1 || die "плагин 'docker compose' (v2) не найден — установите его (legacy 'docker-compose' v1 не поддерживается этим проектом)."

log "2/5 файлы проекта: compose.yaml, Dockerfile, sources.yaml"
for f in compose.yaml Dockerfile sources.yaml; do
    [[ -f "$f" ]] || die "не найден '$f' — запустите скрипт из корня репозитория (сейчас: $(pwd))."
done

log "3/5 .env"
if [[ ! -f .env ]]; then
    log "  .env не найден — используются значения по умолчанию из compose.yaml." \
        "Для production-значений (порты/CORS/reverse proxy, см. .env.example): cp .env.example .env && \$EDITOR .env"
elif [[ -d .git ]] && command -v git >/dev/null 2>&1; then
    # Секреты только через env, не в репозиторий (main-prompt.md §7) — .env
    # обязан быть проигнорирован git, иначе значения рискуют попасть в коммит.
    git check-ignore -q .env || die ".env существует, но НЕ игнорируется git (.gitignore) — секреты рискуют попасть в репозиторий. Добавьте '.env' в .gitignore перед продолжением."
fi

log "4/5 docker compose config (валидация синтаксиса и интерполяции переменных)"
if ! compose_output=$(docker compose config 2>&1); then
    die $'docker compose config провалился — обычно это опечатка в .env или несовместимая версия docker compose.\n'"$compose_output"
fi

log "5/5 порты хоста свободны"
for var_default in "API_PORT:8000" "WEB_PORT:8080"; do
    name="${var_default%%:*}"
    default="${var_default##*:}"
    port="${!name:-$default}"
    if command -v ss >/dev/null 2>&1 && ss -ltn "( sport = :$port )" 2>/dev/null | grep -q ":$port"; then
        die "порт $port (переменная $name, по умолчанию $default) уже занят на хосте — освободите его или задайте $name=<другой порт> в .env."
    fi
done

log "preflight пройден: чистое окружение готово к 'scripts/deploy.sh' (или 'docker compose up --build -d')."
