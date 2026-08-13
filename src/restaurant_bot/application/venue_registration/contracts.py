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

    @property
    def actor_id(self) -> str:
        """Возвращает нейтральный идентификатор участника."""
        return self.telegram_user_id

    @property
    def conversation_id(self) -> str:
        """Возвращает нейтральный идентификатор разговора."""
        return self.telegram_chat_id

    @property
    def channel(self) -> str:
        """Возвращает канал, для которого сохранён контекст."""
        return "telegram"


@dataclass(slots=True)
class RegistrationResult:
    """Описывает результат привязки пользователя к заведению."""

    handled: bool
    reply: RegistrationReply | None = None
    context: VenueContext | None = None
    reset_session: bool = False
