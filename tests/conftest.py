from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("OPENAI_API_KEY", "test-openai")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_FILE", "/tmp/google.json")
os.environ.setdefault("GOOGLE_RECALC_URL", "https://example.test/recalc")
os.environ.setdefault("GOOGLE_ORDER_SUBMISSION_URL", "https://example.test/submit")
os.environ.setdefault("GOOGLE_ORDER_SUBMISSION_SECRET", "test-submit-secret")

from restaurant_bot.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Возвращает изолированные тестовые настройки."""
    return Settings(
        telegram_bot_token="test-token",
        telegram_webhook_secret="test-secret",
        openai_api_key="test-openai",
        google_service_account_file=Path("/tmp/google.json"),
        google_recalc_url="https://example.test/recalc",
        google_order_submission_url="https://example.test/submit",
        google_order_submission_secret="test-submit-secret",
    )
