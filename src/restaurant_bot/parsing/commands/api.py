"""Предоставляет канонический вход для семантического разбора текста."""

from __future__ import annotations

from restaurant_bot.domain.models import ParsedCommand
from restaurant_bot.parsing.commands.dialogue import (
    _standalone_quantity_hint,
    dialogue_response_for,
    retry_requested_for,
)
from restaurant_bot.parsing.commands.router import parse_text_command


def enrich_command(text: str, command: ParsedCommand) -> ParsedCommand:
    """Добавляет к разобранной команде диалоговые метаданные."""
    quantity_hint, quantity_hint_unit = _standalone_quantity_hint(text)
    return command.model_copy(
        update={
            "quantity_hint": quantity_hint,
            "quantity_hint_unit": quantity_hint_unit,
            "retry_requested": retry_requested_for(text),
            "dialogue_response": dialogue_response_for(
                text or command.text,
                command.intent,
                command.items,
            ),
        }
    )


def infer_intent(text: str) -> ParsedCommand:
    """Определяет ParsedCommand для текстового или голосового сообщения."""
    return enrich_command(text, parse_text_command(text))
