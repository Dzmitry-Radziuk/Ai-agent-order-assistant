"""Сохраняет переходный facade старого parser import path."""

from __future__ import annotations

from restaurant_bot.domain.models import ParsedCommand
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.parsing.commands.api import enrich_command
from restaurant_bot.parsing.commands.api import infer_intent as _infer_text_intent
from restaurant_bot.parsing.commands.dialogue import (
    dialogue_response_for,
    retry_requested_for,
)
from restaurant_bot.parsing.commands.item_commands import (
    clean_command_target,
    has_explicit_add_items,
    is_product_add_request_phrase,
)
from restaurant_bot.parsing.commands.normalization import (
    has_negated_action,
    has_negation,
    is_explicit_item_rejection,
    normalize_command_text,
)
from restaurant_bot.parsing.comment_scope import has_explicit_global_comment_scope
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.quantities import parse_quantity_unit

__all__ = [
    "clean_command_target",
    "dialogue_response_for",
    "has_explicit_add_items",
    "has_explicit_global_comment_scope",
    "has_negated_action",
    "has_negation",
    "infer_intent",
    "is_explicit_item_rejection",
    "is_product_add_request_phrase",
    "normalize_command_text",
    "parse_callback",
    "parse_product_lines",
    "parse_quantity_unit",
    "retry_requested_for",
]


def infer_intent(text: str, callback_data: str = "") -> ParsedCommand:
    """Разбирает текст или callback через старый совместимый контракт."""
    if callback_data:
        return enrich_command(text, parse_callback(callback_data))
    return _infer_text_intent(text)
