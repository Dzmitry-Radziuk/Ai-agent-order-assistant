"""Контракты прикладного сценария регистрации и доступа к заведению."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class RegistrationReply(Protocol):
    """Описывает минимальный ответ, который рендерит внешний канал."""

    text: str
    rows: list[list[Any]]
    edit_message_id: int | None
    disable_previous_keyboard: bool
    parse_mode: str


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
    reply: RegistrationReply | None = None
    context: VenueContext | None = None
    reset_session: bool = False
