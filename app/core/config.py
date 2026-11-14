"""Конфигурация приложения из переменных окружения и файла .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Все настройки читаются из окружения; значения по умолчанию подходят для локального запуска."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False
    default_timezone: str = "Europe/Moscow"

    database_url: str = "postgresql+asyncpg://aimarketer:aimarketer@localhost:5433/aimarketer"
    test_database_url: str = "postgresql+asyncpg://aimarketer:aimarketer@localhost:5433/aimarketer_test"
    redis_url: str = "redis://localhost:6380/0"

    telegram_bot_token: SecretStr | None = None
    telegram_proxy: str | None = None

    gigachat_auth_key: SecretStr | None = None
    gigachat_scope: Literal["GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"] = (
        "GIGACHAT_API_PERS"
    )
    gigachat_base_url: str = "https://api.giga.chat/v1"
    gigachat_ca_bundle: Path = BASE_DIR / "certs" / "russian_trusted_ca_bundle.pem"
    gigachat_model_smart: str = "GigaChat-3-Ultra"
    gigachat_model_fast: str = "GigaChat-2"
    gigachat_timeout_s: float = 120.0

    # Очередь к модели: бесплатный тариф GigaChat — один запрос одновременно.
    llm_lock_ttl_s: float = 180.0
    llm_queue_wait_s: float = 120.0

    # Файлы пользователей (в Docker — том /app/data).
    files_dir: Path = BASE_DIR / "data" / "files"
    max_upload_mb: int = 20  # облачный Bot API отдаёт боту файлы не больше 20 МБ

    # Защита от злоупотреблений: сообщений боту в минуту от одного пользователя.
    user_messages_per_minute: int = 20

    @field_validator("telegram_bot_token", "gigachat_auth_key", "telegram_proxy", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        """Пустая строка в .env означает «не задано»."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("default_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Неизвестный часовой пояс: {value}") from exc
        return value


@lru_cache
def get_settings() -> Settings:
    """Единый экземпляр настроек на процесс."""
    return Settings()
