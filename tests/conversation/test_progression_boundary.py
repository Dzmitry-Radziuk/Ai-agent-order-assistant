"""Проверяет поведение, связанное с модулем «test progression boundary»."""

from copy import deepcopy

from restaurant_bot.conversation.progression import ProgressionKind, ProgressionResult
from restaurant_bot.domain.models import CartItem, ConversationState, ItemStatus
from restaurant_bot.presentation.telegram.formatting import russian_plural
from restaurant_bot.presentation.telegram.progression import render_progression
from restaurant_bot.presentation.telegram.replies import issue_reply


def test_progression_renderer_does_not_mutate_state() -> None:
    """Telegram renderer читает state и не меняет результат перехода."""
    state = ConversationState(
        cart=[CartItem(id="one", source_query="Курица", status=ItemStatus.MATCHED)],
        cart_page=3,
    )
    result = ProgressionResult(kind=ProgressionKind.DRAFT, added_count=1)
    before = deepcopy(state.model_dump())

    reply = render_progression(state, result)

    assert "<i>Товар добавлен</i>" in reply.text
    assert "Добавляйте товары текстом, голосом или фотографией списка" in reply.text
    assert state.model_dump() == before


def test_progression_renderer_keeps_add_more_prompt_contract() -> None:
    """Renderer использует существующий Telegram prompt для add-more перехода."""
    state = ConversationState()
    result = ProgressionResult(kind=ProgressionKind.ADD_MORE_CONFIRM, prompt_count=2)

    reply = render_progression(state, result)

    assert "добав" in reply.text.lower()


def test_russian_plural_covers_quantity_boundaries() -> None:
    """Выбирает правильную форму для единиц, нескольких и многих товаров."""
    assert [russian_plural(value, "товар", "товара", "товаров") for value in (1, 21)] == [
        "товар",
        "товар",
    ]
    assert [russian_plural(value, "товар", "товара", "товаров") for value in (2, 4, 22)] == [
        "товара",
        "товара",
        "товара",
    ]
    assert [russian_plural(value, "товар", "товара", "товаров") for value in (5, 11, 14, 25)] == [
        "товаров",
        "товаров",
        "товаров",
        "товаров",
    ]


def test_resumed_issue_reply_counts_only_active_unresolved_items() -> None:
    """Показывает число оставшихся вопросов без учёта решённых и пропущенных позиций."""
    current = CartItem(id="current", source_query="Сыр", status=ItemStatus.MISSING_QTY)
    resolved = CartItem(id="resolved", source_query="Курица", status=ItemStatus.MATCHED)
    skipped = CartItem(id="skipped", source_query="Укроп", status=ItemStatus.SKIPPED)
    state = ConversationState(cart=[current, resolved, skipped])

    reply = issue_reply(current, state=state, resumed=True)

    assert "Осталось уточнить один товар" in reply.text
    assert "<b>Сыр</b>" in reply.text
    assert "Сколько нужно?" in reply.text


def test_resumed_non_quantity_issue_does_not_add_quantity_prompt() -> None:
    """Возобновление карточки кандидатов не подменяется вопросом о количестве."""
    current = CartItem(
        id="current",
        source_query="Сыр",
        status=ItemStatus.AMBIGUOUS,
        candidates=[],
    )
    state = ConversationState(cart=[current])

    reply = issue_reply(current, state=state, resumed=True)

    assert "Осталось уточнить один товар" in reply.text
    assert "Сколько нужно?" not in reply.text
