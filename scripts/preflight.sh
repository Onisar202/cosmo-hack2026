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

log "1/5 инструменты: docker, docker compose, jq"
require_cmd docker
require_cmd jq
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

log "5/5 порты хоста свободны (только при первом развёртывании)"
if [[ -n "$(compose ps -q 2>/dev/null)" ]]; then
    # Контейнеры этого compose-проекта уже существуют — deploy.sh/upgrade.sh/
    # rollback.sh переиспользуют занятые ИМИ ЖЕ порты через `up -d`, это не
    # конфликт. Проверка "порт свободен" имеет смысл только на первом запуске
    # (round 1 ревью PR #36: иначе повторный deploy/upgrade/rollback падал
    # здесь на собственном же работающем стеке).
    log "  compose-проект уже развёрнут — проверка занятости портов пропущена."
else
    # Публикуемые порты берём из `docker compose config` (уже проверен на
    # шаге 4/5), а не из переменных текущей shell-сессии — так учитываются
    # значения из .env, даже если оператор не экспортировал их вручную.
    published_ports="$(docker compose config --format json 2>/dev/null | jq -r '[.services[].ports[]?.published // empty] | .[]' 2>/dev/null || true)"
    for port in $published_ports; do
        if command -v ss >/dev/null 2>&1 && ss -ltn "( sport = :$port )" 2>/dev/null | grep -q ":$port"; then
            die "порт $port уже занят на хосте другим процессом — освободите его или поменяйте порт (API_PORT/WEB_PORT) в .env."
        fi
    done
fi

log "preflight пройден: чистое окружение готово к 'scripts/deploy.sh' (или 'docker compose up --build -d')."
