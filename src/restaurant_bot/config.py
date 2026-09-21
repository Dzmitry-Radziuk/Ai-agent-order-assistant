"""Хранит и проверяет настройки приложения."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def settings_env_file(app_env: str | None = None) -> str | None:
    """Возвращает dotenv-файл выбранного окружения."""
    environment = (app_env or os.getenv("APP_ENV") or "local").strip().lower()
    return ".env" if environment == "production" else None


class Settings(BaseSettings):
    """Задаёт проверенные настройки всех процессов бота."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"
    log_user_content: bool = False
    log_content_max_length: int = Field(default=500, ge=50, le=5000)
    app_timezone: str = "Europe/Minsk"
    public_base_url: str = "http://localhost:8000"

    telegram_bot_token: SecretStr
    telegram_webhook_secret: SecretStr
    telegram_request_timeout_seconds: float = Field(default=15.0, ge=5.0, le=60.0)
    telegram_connect_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)

    openai_api_key: SecretStr
    openai_text_model: str = "gpt-4o-mini"
    openai_text_timeout_seconds: float = Field(default=30.0, ge=3.0, le=60.0)
    openai_text_max_retries: int = Field(default=1, ge=0, le=2)
    openai_vision_model: str = "gpt-5.4-mini-2026-03-17"
    openai_vision_timeout_seconds: float = Field(default=180.0, ge=30.0, le=600.0)
    openai_match_model: str = "gpt-4o-mini"
    openai_transcribe_model: str = "gpt-4o-transcribe"
    openai_transcribe_fallback_model: str = "gpt-4o-transcribe"

    database_url: str = "postgresql+psycopg://bot:bot@localhost:5432/bot"
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    database_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    order_event_retention_days: int = Field(default=365, ge=30, le=3650)
    telegram_update_retention_days: int = Field(default=30, ge=7, le=365)
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    google_service_account_file: Path
    google_venue_directory_url: str = ""
    google_registration_spreadsheet_id: str = ""
    google_registration_sheet: str = "Чаты"
    venue_directory_cache_ttl_seconds: int = Field(default=120, ge=30, le=3600)
    venue_access_cache_ttl_seconds: int = Field(default=60, ge=15, le=3600)
    google_catalog_sheet: str = "Заявка"
    google_order_status_sheet: str = "История"
    google_product_add_sheet: str = "Добавить"
    google_recalc_url: str
    google_recalc_token: SecretStr = SecretStr("")
    google_recalc_sheet: str = "Заявка"
    google_order_submission_enabled: bool = False
    google_order_submission_url: str
    google_order_submission_secret: SecretStr
    google_order_submission_timeout_seconds: float = Field(default=60.0, ge=5.0, le=180.0)
    catalog_cache_ttl_seconds: int = Field(default=3600, ge=5, le=3600)

    default_department: str = "Кухня"

    langfuse_enabled: bool = False
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str | None = None
    langfuse_tracing_environment: str = "production"
    langfuse_flush_at: int = Field(default=32, ge=1, le=512)
    langfuse_flush_interval_seconds: float = Field(default=2.0, ge=0.1, le=60.0)

    @field_validator("public_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        """Удаляет завершающий слеш из публичного URL."""
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_production_settings(self) -> Settings:
        """Отклоняет небезопасные production-настройки."""
        if self.app_env != "production":
            return self
        if not self.public_base_url.startswith("https://") or "example." in self.public_base_url:
            raise ValueError("PUBLIC_BASE_URL must use HTTPS in production")
        webhook_secret = self.telegram_webhook_secret.get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", webhook_secret):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must contain 32-256 URL-safe characters")
        required_secrets = {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token.get_secret_value(),
            "OPENAI_API_KEY": self.openai_api_key.get_secret_value(),
            "GOOGLE_RECALC_TOKEN": self.google_recalc_token.get_secret_value(),
            "GOOGLE_ORDER_SUBMISSION_SECRET": (
                self.google_order_submission_secret.get_secret_value()
            ),
        }
        placeholders = [
            name
            for name, value in required_secrets.items()
            if not value or value.startswith("replace_")
        ]
        if placeholders:
            raise ValueError(f"Production secrets are not configured: {', '.join(placeholders)}")
        required_config = {
            "GOOGLE_VENUE_DIRECTORY_URL": self.google_venue_directory_url,
            "GOOGLE_REGISTRATION_SPREADSHEET_ID": self.google_registration_spreadsheet_id,
            "GOOGLE_RECALC_URL": self.google_recalc_url,
            "GOOGLE_ORDER_SUBMISSION_URL": self.google_order_submission_url,
        }
        missing_config = [
            name
            for name, value in required_config.items()
            if not value or "replace_" in value or "example." in value
        ]
        if missing_config:
            raise ValueError(
                f"Production configuration is not complete: {', '.join(missing_config)}"
            )
        if not self.google_venue_directory_url.startswith("https://"):
            raise ValueError("GOOGLE_VENUE_DIRECTORY_URL must use HTTPS in production")
        if not self.google_recalc_url.startswith("https://"):
            raise ValueError("GOOGLE_RECALC_URL must use HTTPS in production")
        if not self.google_order_submission_url.startswith("https://"):
            raise ValueError("GOOGLE_ORDER_SUBMISSION_URL must use HTTPS in production")
        if "bot:bot@" in self.database_url or "replace_" in self.database_url:
            raise ValueError("DATABASE_URL must contain production database credentials")
        if self.langfuse_enabled and not (
            self.langfuse_public_key and self.langfuse_secret_key and self.langfuse_base_url
        ):
            raise ValueError("Langfuse credentials must be configured when tracing is enabled")
        return self

    @property
    def telegram_webhook_url(self) -> str:
        """Возвращает полный URL webhook Telegram."""
        return f"{self.public_base_url}/webhooks/telegram"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Загружает и кэширует настройки окружения."""
    return Settings(_env_file=settings_env_file(), _env_file_encoding="utf-8")
