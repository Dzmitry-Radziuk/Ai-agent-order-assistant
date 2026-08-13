"""Рендерит нейтральный результат в Telegram-ответ."""

from __future__ import annotations

from restaurant_bot.application.conversation.actions import (
    decode_action_token,
    encode_action_token,
)
from restaurant_bot.application.conversation.contracts import ConversationView, SemanticAction
from restaurant_bot.domain.models import BotReply, Button


def telegram_action_mapper(button: Button, group: int) -> SemanticAction:
    """Преобразует Telegram-кнопку в нейтральное действие."""
    return decode_action_token(button.callback_data, button.text, group)


def render_conversation_view(view: ConversationView) -> BotReply:
    """Преобразует смысловые действия в совместимые Telegram-кнопки."""
    rows: dict[int, list[Button]] = {}
    for action in view.actions:
        rows.setdefault(action.group, []).append(
            Button(text=action.label, callback_data=encode_action_token(action))
        )
    return BotReply(text=view.text, rows=[rows[index] for index in sorted(rows)])
