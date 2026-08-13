"""Проверяет поведение, связанное с модулем «test logging security»."""

import logging

from restaurant_bot.observability import configure_logging, sanitize_log_value


def test_http_client_logs_are_suppressed_to_protect_telegram_token() -> None:
    """Проверяет, что HTTP клиент logs являются suppressed в protect Telegram токен."""
    configure_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_log_sanitizer_redacts_nested_secrets_and_tokens_in_urls() -> None:
    """Проверяет, что log очистка журнала redacts nested секреты и токены в urls."""
    payload = {
        "telegram_bot_token": "123456:secret",
        "nested": {
            "url": "https://api.telegram.org/bot123456:secret/getMe",
            "authorization": "Bearer private-token",
        },
    }

    sanitized = sanitize_log_value(payload)

    assert sanitized["telegram_bot_token"] == "<redacted>"
    assert sanitized["nested"]["authorization"] == "<redacted>"
    assert sanitized["nested"]["url"] == "https://api.telegram.org/bot<redacted>/getMe"


def test_log_sanitizer_hashes_identifiers() -> None:
    """Проверяет, что log очистка журнала hashes identifiers."""
    sanitized = sanitize_log_value("8007761661", key="chat_id")

    assert sanitized.startswith("sha256:")
    assert "8007761661" not in sanitized


def test_user_content_is_hidden_by_default() -> None:
    """Проверяет, что содержимое пользовательских сообщений по умолчанию скрыто."""
    sanitized = sanitize_log_value("Сироп Роза 10 штук", key="text")

    assert sanitized.startswith("<content sha256:")
    assert "Сироп" not in sanitized
    assert "length:" in sanitized


def test_user_content_can_be_enabled_and_is_bounded() -> None:
    """Проверяет, что пользователь content может be включено и является ограничено."""
    sanitized = sanitize_log_value(
        "Очень длинный пользовательский текст",
        key="text",
        include_user_content=True,
        max_content_length=10,
    )

    assert sanitized.startswith("Очень длин")
    assert "<truncated:" in sanitized


def test_non_secret_token_metrics_are_preserved() -> None:
    """Проверяет, что не секрет токен метрики являются сохраняется."""
    assert sanitize_log_value(5000, key="max_output_tokens") == 5000
