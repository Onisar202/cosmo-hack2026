"""Точка сборки FastAPI-приложения.

На этом этапе контракт ограничен health-проверкой: расчётные эндпоинты
появятся вместе со схемами в ``contracts/`` в последующих задачах.
"""

from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from src.config import get_settings


class HealthResponse(BaseModel):
    status: str
    service: str
    app_env: str
    time: datetime


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.service_name)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=settings.service_name,
            app_env=settings.app_env,
            time=datetime.now(timezone.utc),
        )

    return app


app = create_app()


def main() -> None:
    """Единая команда локального запуска: ``uv run python -m src.api.app``."""
    settings = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
