"""Проверяет поведение, связанное с модулем «test cli»."""

import sys

from restaurant_bot import cli


def test_set_webhook_command_calls_telegram_api(settings, mocker, capsys) -> None:  # type: ignore[no-untyped-def]
    """Настраивает webhook с секретом из конфигурации."""
    mocker.patch.object(sys, "argv", ["restaurant-bot", "set-webhook"])
    mocker.patch.object(cli, "get_settings", return_value=settings)
    telegram_cls = mocker.patch.object(cli, "TelegramClient")

    cli.main()

    telegram_cls.return_value.set_webhook.assert_called_once_with(
        settings.telegram_webhook_url,
        settings.telegram_webhook_secret.get_secret_value(),
    )
    assert "Webhook configured" in capsys.readouterr().out


def test_sync_bindings_command_imports_google_registry(settings, mocker, capsys) -> None:  # type: ignore[no-untyped-def]
    """Импортирует реестр привязок через административную команду."""
    mocker.patch.object(sys, "argv", ["restaurant-bot", "sync-venue-bindings"])
    mocker.patch.object(cli, "get_settings", return_value=settings)
    redis_cls = mocker.patch.object(cli, "Redis")
    sheets_cls = mocker.patch.object(cli, "GoogleSheetsGateway")
    registration_cls = mocker.patch.object(cli, "VenueRegistrationService")
    registration_cls.return_value.bootstrap_existing_bindings.return_value = 3

    cli.main()

    redis_cls.from_url.assert_called_once_with(settings.redis_url, decode_responses=True)
    sheets_cls.assert_called_once_with(settings)
    registration_cls.assert_called_once_with(
        settings,
        redis_cls.from_url.return_value,
        sheets_cls.return_value,
    )
    assert "Venue bindings imported: 3" in capsys.readouterr().out
