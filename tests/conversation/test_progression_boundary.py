from copy import deepcopy

from restaurant_bot.conversation.progression import ProgressionKind, ProgressionResult
from restaurant_bot.domain.models import CartItem, ConversationState, ItemStatus
from restaurant_bot.presentation.telegram.progression import render_progression


def test_progression_renderer_does_not_mutate_state() -> None:
    """Telegram renderer читает state и не меняет результат перехода."""
    state = ConversationState(
        cart=[CartItem(id="one", source_query="Курица", status=ItemStatus.MATCHED)],
        cart_page=3,
    )
    result = ProgressionResult(kind=ProgressionKind.DRAFT, added_count=1)
    before = deepcopy(state.model_dump())

    reply = render_progression(state, result)

    assert "Добавлено позиций: 1" in reply.text
    assert state.model_dump() == before


def test_progression_renderer_keeps_add_more_prompt_contract() -> None:
    """Renderer использует существующий Telegram prompt для add-more перехода."""
    state = ConversationState()
    result = ProgressionResult(kind=ProgressionKind.ADD_MORE_CONFIRM, prompt_count=2)

    reply = render_progression(state, result)

    assert "добав" in reply.text.lower()
