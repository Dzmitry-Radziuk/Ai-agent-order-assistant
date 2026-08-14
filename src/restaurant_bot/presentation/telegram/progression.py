"""Строит Telegram-ответ для уже выполненного перехода черновика."""

from __future__ import annotations

from restaurant_bot.conversation.progression import ProgressionKind, ProgressionResult
from restaurant_bot.conversation.state.queries import item_index
from restaurant_bot.domain.models import BotReply, ConversationState
from restaurant_bot.presentation.telegram.replies import (
    added_items_question_reply,
    cart_reply,
    issue_reply,
)


def render_progression(state: ConversationState, result: ProgressionResult) -> BotReply:
    """Преобразует channel-neutral результат перехода в Telegram-ответ."""
    if result.kind is ProgressionKind.ISSUE:
        assert result.item is not None
        return issue_reply(
            result.item,
            item_index(state, result.item),
            state=state,
            resumed=result.resumed,
        )
    if result.kind is ProgressionKind.ADD_MORE_CONFIRM:
        return added_items_question_reply(state, result.prompt_count)
    title = f"Добавлено позиций: {result.added_count}" if result.added_count else "Черновик заявки"
    return cart_reply(state, title=title)
