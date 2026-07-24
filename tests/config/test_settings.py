from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from restaurant_bot.config import Settings, get_settings, settings_env_file


def test_only_production_selects_dotenv_file() -> None:
    assert settings_env_file("production") == ".env"
    assert settings_env_file("local") is None
    assert settings_env_file("test") is None


def test_user_content_logging_is_private_by_default() -> None:
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


def test_get_settings_reads_dotenv_in_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.app_env == "production"
    assert settings.telegram_bot_token.get_secret_value() == "telegram-secret"
    get_settings.cache_clear()


def test_production_rejects_an_insecure_public_url() -> None:
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
