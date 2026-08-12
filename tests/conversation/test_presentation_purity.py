from copy import deepcopy

from restaurant_bot.domain.models import CartItem, ConversationState, ItemStatus
from restaurant_bot.presentation.telegram.replies import (
    cart_reply,
    final_review_reply,
    welcome_reply,
)


def test_telegram_presenters_do_not_mutate_conversation_state() -> None:
    """Проверяет, что Telegram-презентеры только читают состояние диалога."""
    state = ConversationState(
        metadata={"onboarding_shown": False},
        cart_page=4,
        final_review_page=4,
        cart=[
            CartItem(
                id="item",
                source_query="Товар",
                quantity=1,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
        ],
    )
    before = deepcopy(state)

    welcome_reply(state)
    cart_reply(state)
    final_review_reply(state)

    assert state == before
