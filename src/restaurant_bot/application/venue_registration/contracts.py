"""Контракты прикладного сценария регистрации и доступа к заведению."""

from __future__ import annotations

from dataclasses import dataclass

from restaurant_bot.domain.models import BotReply


@dataclass(slots=True, frozen=True)
class VenueContext:
    """Передаёт контекст активного заведения при обработке."""

    venue_code: str
    venue_name: str
    spreadsheet_id: str
    spreadsheet_url: str
    telegram_user_id: str
    telegram_chat_id: str


@dataclass(slots=True)
class RegistrationResult:
    """Описывает результат привязки пользователя к заведению."""

    handled: bool
    reply: BotReply | None = None
    context: VenueContext | None = None
    reset_session: bool = False
