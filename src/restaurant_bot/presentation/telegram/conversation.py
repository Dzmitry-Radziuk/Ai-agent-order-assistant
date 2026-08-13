"""Рендерит нейтральный результат в Telegram-ответ."""

from __future__ import annotations

from restaurant_bot.application.conversation.contracts import ConversationView
from restaurant_bot.domain.models import BotReply, Button


def render_conversation_view(view: ConversationView) -> BotReply:
    """Преобразует смысловые действия в совместимые Telegram-кнопки."""
    rows: dict[int, list[Button]] = {}
    for action in view.actions:
        rows.setdefault(action.group, []).append(
            Button(text=action.label, callback_data=action.action_id)
        )
    return BotReply(text=view.text, rows=[rows[index] for index in sorted(rows)])
