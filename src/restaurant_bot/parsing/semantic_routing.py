"""Защищает подтверждённую семантику команды от небезопасного ответа ИИ."""

from __future__ import annotations

from restaurant_bot.domain.models import Intent, ParsedCommand
from restaurant_bot.parsing.comment_scope import has_explicit_order_comment_scope


def normalize_comment_proposal(source_text: str, command: ParsedCommand) -> ParsedCommand:
    """Превращает явный общий комментарий без товаров в безопасную мутацию черновика."""
    if (
        command.global_comment
        and not command.items
        and has_explicit_order_comment_scope(source_text)
    ):
        return command.model_copy(
            update={
                "intent": Intent.EDIT_COMMENT,
                "comment_action": "add",
                "comment_scope": "order",
                "comment_text": command.global_comment,
                "global_comment": "",
                "explicit_add_items": False,
            }
        )
    return command


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
