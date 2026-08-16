"""Защищает подтверждённую семантику команды от небезопасного ответа ИИ."""

from __future__ import annotations

from restaurant_bot.domain.models import Intent, ParsedCommand


def protect_confirmed_command(
    deterministic: ParsedCommand,
    proposed: ParsedCommand,
) -> ParsedCommand:
    """Сохраняет доказанную команду, если ИИ потерял её товарную структуру."""
    if deterministic.intent is Intent.EDIT_COMMENT:
        return deterministic
    if (
        deterministic.intent is Intent.ADD_ITEMS
        and deterministic.items
        and (
            proposed.intent is Intent.HISTORY_QUERY
            or proposed.intent is not Intent.ADD_ITEMS
            or not proposed.items
        )
    ):
        return deterministic
    return proposed
