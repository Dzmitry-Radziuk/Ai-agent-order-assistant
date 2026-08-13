"""Запускает консольные команды приложения."""

from __future__ import annotations

import argparse

from redis import Redis

from restaurant_bot.config import get_settings
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.integrations.telegram import TelegramClient
from restaurant_bot.services.venue_registration import VenueRegistrationService


def main() -> None:
    """Запускает выбранную административную команду."""
    parser = argparse.ArgumentParser(description="Restaurant procurement bot administration")
    parser.add_argument("command", choices=["set-webhook", "sync-venue-bindings"])
    args = parser.parse_args()
    settings = get_settings()
    if args.command == "set-webhook":
        TelegramClient(settings).set_webhook(
            settings.telegram_webhook_url,
            settings.telegram_webhook_secret.get_secret_value(),
        )
        print(f"Webhook configured: {settings.telegram_webhook_url}")
    elif args.command == "sync-venue-bindings":
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        sheets = GoogleSheetsGateway(settings)
        imported = VenueRegistrationService(
            settings,
            redis,
            sheets,
        ).bootstrap_existing_bindings()
        print(f"Venue bindings imported: {imported}")


if __name__ == "__main__":
    main()
