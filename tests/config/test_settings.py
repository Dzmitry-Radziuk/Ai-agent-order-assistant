from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from restaurant_bot.config import Settings, get_settings, settings_env_file


def test_only_production_selects_dotenv_file() -> None:
    """Проверяет, что только production выбирает dotenv файл."""
    assert settings_env_file("production") == ".env"
    assert settings_env_file("local") is None
    assert settings_env_file("test") is None


def test_user_content_logging_is_private_by_default() -> None:
    """Проверяет, что пользователь content логирование является личный by по умолчанию."""
    settings = Settings(
        telegram_bot_token="test-token",
        telegram_webhook_secret="test-secret",
        openai_api_key="test-openai",
        google_service_account_file=Path("/tmp/google.json"),
        google_recalc_url="https://example.test/recalc",
    )

    assert settings.log_user_content is False
    assert settings.log_content_max_length == 500
    assert settings.order_event_retention_days == 365
    assert settings.telegram_update_retention_days == 30
    assert settings.telegram_request_timeout_seconds == 15
    assert settings.telegram_connect_timeout_seconds == 5
    assert settings.openai_text_timeout_seconds == 30
    assert settings.venue_access_cache_ttl_seconds == 60


def test_get_settings_reads_dotenv_in_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Проверяет, что get settings читает dotenv в production."""
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "APP_ENV=production",
                "PUBLIC_BASE_URL=https://bot.company.test",
                "TELEGRAM_BOT_TOKEN=telegram-secret",
                "TELEGRAM_WEBHOOK_SECRET=12345678901234567890123456789012",
                "OPENAI_API_KEY=openai-secret",
                "GOOGLE_SERVICE_ACCOUNT_FILE=/run/secrets/google.json",
                "GOOGLE_VENUE_DIRECTORY_URL=https://docs.google.com/spreadsheets/d/sheet/gviz/tq",
                "GOOGLE_REGISTRATION_SPREADSHEET_ID=registration-sheet-id",
                "GOOGLE_RECALC_URL=https://script.google.com/macros/s/script-id/exec",
                "GOOGLE_RECALC_TOKEN=recalc-secret",
                "GOOGLE_ORDER_SUBMISSION_ENABLED=false",
                "GOOGLE_ORDER_SUBMISSION_URL=https://script.google.com/macros/s/submit-id/exec",
                "GOOGLE_ORDER_SUBMISSION_SECRET=submit-secret",
                "DATABASE_URL=postgresql+psycopg://bot:strong-password@postgres:5432/bot",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "production")
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "OPENAI_API_KEY",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "GOOGLE_VENUE_DIRECTORY_URL",
        "GOOGLE_REGISTRATION_SPREADSHEET_ID",
        "GOOGLE_RECALC_URL",
        "GOOGLE_ORDER_SUBMISSION_ENABLED",
        "GOOGLE_ORDER_SUBMISSION_URL",
        "GOOGLE_ORDER_SUBMISSION_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.app_env == "production"
    assert settings.telegram_bot_token.get_secret_value() == "telegram-secret"
    assert settings.google_order_submission_enabled is False
    get_settings.cache_clear()


def test_production_rejects_an_insecure_public_url() -> None:
    """Проверяет, что production отклоняет an небезопасный public URL."""
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(
            app_env="production",
            public_base_url="http://bot.example.test",
            telegram_bot_token="telegram-secret",
            telegram_webhook_secret="12345678901234567890123456789012",
            openai_api_key="openai-secret",
            google_service_account_file=Path("/run/secrets/google.json"),
            google_recalc_url="https://example.test/recalc",
            google_recalc_token="recalc-secret",
        )


def test_production_rejects_placeholder_google_configuration() -> None:
    """Проверяет, что production отклоняет заглушка Google configuration."""
    with pytest.raises(ValidationError, match="configuration is not complete"):
        Settings(
            app_env="production",
            public_base_url="https://bot.company.test",
            telegram_bot_token="telegram-secret",
            telegram_webhook_secret="12345678901234567890123456789012",
            openai_api_key="openai-secret",
            google_service_account_file=Path("/run/secrets/google.json"),
            google_venue_directory_url="https://docs.google.com/spreadsheets/d/replace_me",
            google_registration_spreadsheet_id="replace_me",
            google_recalc_url="https://script.google.com/macros/s/replace_me/exec",
            google_recalc_token="recalc-secret",
            database_url="postgresql+psycopg://bot:strong-password@postgres:5432/bot",
        )


def test_production_rejects_placeholder_submission_secret() -> None:
    """Не запускает production без секрета центрального скрипта."""
    with pytest.raises(ValidationError, match="GOOGLE_ORDER_SUBMISSION_SECRET"):
        Settings(
            app_env="production",
            public_base_url="https://bot.company.test",
            telegram_bot_token="telegram-secret",
            telegram_webhook_secret="12345678901234567890123456789012",
            openai_api_key="openai-secret",
            google_service_account_file=Path("/run/secrets/google.json"),
            google_venue_directory_url="https://docs.google.com/spreadsheets/d/sheet/gviz/tq",
            google_registration_spreadsheet_id="registration-sheet-id",
            google_recalc_url="https://script.google.com/macros/s/recalc-id/exec",
            google_recalc_token="recalc-secret",
            google_order_submission_url="https://script.google.com/macros/s/submit-id/exec",
            google_order_submission_secret="replace_me",
            database_url="postgresql+psycopg://bot:strong-password@postgres:5432/bot",
        )


def test_production_allows_disabled_order_submission() -> None:
    """Запускает production с безопасно отключённой отправкой заявок."""
    settings = Settings(
        app_env="production",
        public_base_url="https://bot.company.test",
        telegram_bot_token="telegram-secret",
        telegram_webhook_secret="12345678901234567890123456789012",
        openai_api_key="openai-secret",
        google_service_account_file=Path("/run/secrets/google.json"),
        google_venue_directory_url="https://docs.google.com/spreadsheets/d/sheet/gviz/tq",
        google_registration_spreadsheet_id="registration-sheet-id",
        google_recalc_url="https://script.google.com/macros/s/recalc-id/exec",
        google_recalc_token="recalc-secret",
        google_order_submission_enabled=False,
        google_order_submission_url="https://script.google.com/macros/s/submit-id/exec",
        google_order_submission_secret="submit-secret",
        database_url="postgresql+psycopg://bot:strong-password@postgres:5432/bot",
    )

    assert settings.google_order_submission_enabled is False
