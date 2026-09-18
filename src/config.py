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


@lru_cache
def get_settings() -> Settings:
    """Строит и кеширует настройки. Первый вызов — при создании приложения."""
    # Обязательные поля заполняются pydantic-settings из окружения/.env,
    # а не позиционными аргументами — это не видно статическому анализу.
    return Settings()  # type: ignore[call-arg]
