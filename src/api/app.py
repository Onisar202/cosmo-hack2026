"""Точка сборки FastAPI-приложения.

Помимо health-проверки, собирает расчётный API S1-07
(``src/api/routes.py``): запуск расчёта, статус задачи, сохранённые
результаты, статусы и принудительное обновление источников
(``contracts/README.md``, «Endpoint shapes для S1-07»).
"""

from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.jobs import JobRegistry
from src.api.routes import router as api_router
from src.api.service import ApiError, ensure_store_ready
from src.config import get_settings
from src.sources.status import SourceStatusRegistry
from src.store import RawOriginalStore


class HealthResponse(BaseModel):
    status: str
    service: str
    app_env: str
    time: datetime


def _error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.service_name)

    # Общее для всех запросов состояние процесса (.ai/backend-prompt.md §7
    # «зависимости через Depends(), конфигурация — объект настроек»):
    # реестр задач и реестр статусов источников — единственное разделяемое
    # состояние, оба потокобезопасны и не хранят параметры расчёта
    # (.ai/backend-prompt.md §2). Соединение с SQLite намеренно НЕ хранится
    # здесь — каждый запрос/фоновая задача открывает своё (src/api/service.py),
    # чтобы конкурентные расчёты не делили один sqlite3.Connection.
    app.state.settings = settings
    app.state.raw_store = RawOriginalStore(settings.store_raw_dir)
    app.state.source_registry = SourceStatusRegistry()
    app.state.job_registry = JobRegistry()
    # Создаёт файл/схему хранилища один раз, синхронно, до того как первый
    # конкурентный запрос успеет породить гонку за его создание
    # (src/api/service.py:ensure_store_ready).
    ensure_store_ready(settings)

    app.include_router(api_router)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=settings.service_name,
            app_env=settings.app_env,
            time=datetime.now(timezone.utc),
        )

    @app.exception_handler(ApiError)
    def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=_error_body(exc.code, exc.message)
        )

    @app.exception_handler(RequestValidationError)
    def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Единый формат ошибок без трассировок (.ai/backend-prompt.md §6):
        # FastAPI/Pydantic по умолчанию отдают {"detail": [...]}, здесь —
        # {"error": {"code", "message"}}, как для остальных ошибок API.
        return JSONResponse(status_code=422, content=_error_body("invalid_request", str(exc)))

    return app


app = create_app()


def main() -> None:
    """Единая команда локального запуска: ``uv run python -m src.api.app``."""
    settings = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
