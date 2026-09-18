# Сервис прогнозирования внешних рисков ВКД

Исследовательский прототип поддержки решений для планирования выхода в
открытый космос (ВКД) на МКС: сопоставляет космическую погоду и
метеороидную обстановку с окнами операции. Предметная постановка и
критерии оценки — `.ai/main-prompt.md` (§11, §12).

Этот этап (FN-19) сдаёт только каркас: слоистую структуру Python/FastAPI
сервиса, health-проверку и воспроизводимые проверки качества кода.
Расчётных эндпоинтов и схем контракта ещё нет — они появятся вместе с
последующими задачами.

## Стек

Python 3.11+, FastAPI, Pydantic v2, [uv](https://docs.astral.sh/uv/) как
менеджер пакетов и виртуальных окружений.

## Установка

```bash
uv sync --locked --all-groups
```

Команда создаёт `.venv` и ставит зависимости строго по `uv.lock`.

## Настройка

```bash
cp .env.example .env
```

Обязательные и необязательные переменные окружения описаны с
комментариями в `.env.example`. Значений ключей и других секретов там
нет — на этом этапе сервис не обращается к внешним источникам.
Обязательные настройки (например, `APP_ENV`) проверяются при создании
`Settings` (`src/config.py`), то есть при старте приложения: некорректный
или неполный `.env` не даёт сервису запуститься.

## Запуск

Одна команда локального запуска:

```bash
uv run python -m src.api.app
```

Сервис поднимается на `http://0.0.0.0:8000`. Проверка:

```bash
curl http://127.0.0.1:8000/health
```

```json
{"status":"ok","service":"vkd-risk-service","app_env":"development","time":"2026-09-18T21:30:58.540824Z"}
```

## Проверки

Те же проверки использует CI (`.github/workflows/ci.yml`).

```bash
uv run ruff check .    # линтер
uv run mypy src         # проверка типов
uv run pytest -v         # тесты (без сети)
```

Результаты на чистом checkout (Python 3.11.15, зависимости из `uv.lock`):

```text
$ uv run ruff check .
All checks passed!

$ uv run mypy src
Success: no issues found in 13 source files

$ uv run pytest -v
tests/test_health.py::test_health_returns_200_ok PASSED
tests/test_health.py::test_health_time_is_utc_aware PASSED
tests/test_health.py::test_settings_requires_app_env PASSED
tests/test_health.py::test_settings_rejects_unknown_app_env PASSED
4 passed
```

## Структура проекта

Полное описание слоёв и границ между ними — `.ai/main-prompt.md`, §8. На
этом этапе создан каркас пакетов; наполнение — в последующих задачах по
зонам ответственности.

```text
src/
├── api/        # FastAPI: тонкие роутеры, валидация — сейчас только /health
├── config.py   # Настройки из env, без секретов
├── sources/    # Получение и нормализация (пока не реализовано)
├── store/      # Хранение, версии, выборка по as_of (пока не реализовано)
├── domain/     # Расчёты: spaceweather, orbit, mmod, lighting, windows
└── export/     # HTML и JSON из сохранённого результата (пока не реализовано)
tests/
└── test_health.py
```

Слой `web/` (React + TypeScript + Vite) — зона ответственности 4, в эту
задачу не входит.
