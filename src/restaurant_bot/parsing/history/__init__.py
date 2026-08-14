"""Разбирает естественные вопросы пользователя по истории поставок."""

from restaurant_bot.parsing.history.query import parse_history_query, requires_history_context

__all__ = ["parse_history_query", "requires_history_context"]
