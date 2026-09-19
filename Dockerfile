# syntax=docker/dockerfile:1
#
# Многостадийная сборка сервиса FN-28 (S1-10): один Dockerfile, две цели
# (`api`, `web`), выбираемые в compose.yaml через `build.target`
# (.ai/main-prompt.md §8 «границы слоёв» — контейнер расчёта не подаёт
# статику, контейнер веба не считает).

# ---------------------------------------------------------------------------
# api: FastAPI-сервис (src/, contracts/, sources.yaml) под uv, без dev-групп.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS api

WORKDIR /app

# curl — только для HEALTHCHECK контейнера, не рантайм-зависимость сервиса.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/

# Слой зависимостей кешируется отдельно от исходников: pyproject.toml/uv.lock
# меняются реже, чем src/ (tool.uv package = false — `uv sync` не требует
# исходников пакета на этом шаге).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev

COPY src ./src
COPY contracts ./contracts
COPY sources.yaml ./sources.yaml

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# STORE_DB_PATH/STORE_RAW_DIR по умолчанию относительны (data/...,
# src/config.py) — совпадают с точкой монтирования тома в compose.yaml,
# перезапуск контейнера не теряет сохранённые записи и результаты.
VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["python", "-m", "src.api.app"]

# ---------------------------------------------------------------------------
# web-build: сборка статики React/Vite (web/) — промежуточная стадия,
# в конечный образ не попадает.
# ---------------------------------------------------------------------------
FROM node:22-slim AS web-build

WORKDIR /app/web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web ./
RUN npm run build

# ---------------------------------------------------------------------------
# web: статика за nginx, реверс-прокси /api и /health на сервис api по
# имени в compose-сети (docker/nginx.conf.template).
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS web

# .template в /etc/nginx/templates/ — встроенный docker-entrypoint базового
# образа рендерит его в /etc/nginx/conf.d/default.conf через envsubst при
# каждом старте контейнера (FN-45: reverse proxy target и server_name
# настраиваются переменными окружения compose.yaml без пересборки образа).
# ENV ниже — значения по умолчанию, совпадающие с прежним статическим
# конфигом; compose.yaml может переопределить их без правки Dockerfile.
ENV API_UPSTREAM=api:8000 \
    NGINX_SERVER_NAME=_

COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=web-build /app/web/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD wget -qO- http://localhost/ >/dev/null || exit 1
