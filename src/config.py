"""Настройки сервиса, читаемые из переменных окружения (см. .env.example).

Ни одна настройка не хранит секреты: учётные данные внешних источников
относятся к слою ``sources/`` и добавляются вместе с соответствующими
коннекторами. Обязательные настройки объявлены без значения по умолчанию,
поэтому некорректный или неполный env приводит к ошибке уже при создании
``Settings`` — то есть при старте приложения, а не при первом запросе.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["development", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Обязательная настройка: явное окружение запуска без значения по умолчанию.
    app_env: AppEnv

    # Необязательные настройки с безопасными значениями по умолчанию.
    log_level: LogLevel = "INFO"
    service_name: str = "vkd-risk-service"

    # Пути хранилища (.ai/main-prompt.md §7: адреса и пороги — в конфиге, не в
    # коде). По умолчанию — подкаталог тома проекта, не временная директория:
    # перезапуск сервиса не должен терять сохранённые записи и результаты.
    store_db_path: str = "data/store.sqlite3"
    store_raw_dir: str = "data/raw"

    # Шаг расчёта траектории (.ai/main-prompt.md §6: «шаг расчёта траектории —
    # параметр конфигурации, а не константа в коде»), src/domain/orbit/propagate.py.
    orbit_step_minutes: float = 5.0
    # Насколько давние (или «из будущего») орбитальные элементы ещё считаются
    # подтверждёнными для расчёта; свыше этого порога геометрия помечается как
    # реконструкция (src/domain/orbit/propagate.py:is_reconstructed_geometry,
    # .ai/main-prompt.md §11 «Траектория»).
    orbit_max_confirmed_age_hours: float = 24.0

    # Production deployment (FN-45): источники и UI могут оказаться на разных
    # origin (площадка ещё не выбрана) — без этого браузер молча блокирует
    # запросы UI к API политикой same-origin, и это не видно на localhost, где
    # UI ходит через relative `/api` за тем же nginx-прокси (docker/nginx.conf.template).
    # Пусто по умолчанию: middleware не добавляется вовсе (см. src/api/app.py),
    # а не добавляется с пустым allow-list — иначе поведение FastAPI/Starlette
    # для пустого списка происхождений (реально запрещает всё) сложно отличить
    # от «CORS не настроен» на ревью и в логах.
    cors_allowed_origins: str = ""

    def cors_origins(self) -> list[str]:
        """Список разрешённых origin из `CORS_ALLOWED_ORIGINS` (через запятую).

        Пустые элементы (лишние запятые/пробелы) отбрасываются, а не
        превращаются в разрешение origin `""`.
        """
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Строит и кеширует настройки. Первый вызов — при создании приложения."""
    # Обязательные поля заполняются pydantic-settings из окружения/.env,
    # а не позиционными аргументами — это не видно статическому анализу.
    return Settings()  # type: ignore[call-arg]
