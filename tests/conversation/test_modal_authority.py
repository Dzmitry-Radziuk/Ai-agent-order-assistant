"""Проверяет приоритет активного modal-контекста над слабыми intent."""

from unittest.mock import Mock

import pytest

from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.conversation_handlers.pending_quantity import (
    PendingQuantityHandler,
)
from restaurant_bot.services.engine import ConversationEngine


def _interpreter(parsed: ParsedCommand | None = None) -> TelegramInputInterpreter:
    """Создаёт интерпретатор с детерминированным ответом провайдера."""
    provider = Mock()
    provider.parse_text.side_effect = lambda text: parsed or infer_intent(text)
    provider.choose_visible_action.return_value = ""
    return TelegramInputInterpreter(provider, lambda: Mock(), StateCompatibilityPolicy())


def _quantity_state(status: ItemStatus) -> ConversationState:
    """Создаёт состояние с выбранным товаром и открытым количеством."""
    cart_item = CartItem(
        id="current",
        source_query="Курица",
        catalog_product_id="chicken",
        catalog_name="Курица",
        catalog_unit="кг",
        status=status,
    )
    return ConversationState(
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
        current_issue_item_id=cart_item.id,
        cart=[cart_item],
    )


@pytest.mark.parametrize("input_kind", [InputKind.TEXT, InputKind.VOICE])
@pytest.mark.parametrize(
    ("phrase", "expected_quantity", "expected_unit"),
    [
        ("5", 5, "кг"),
        ("пять", 5, "кг"),
        ("пусть будет пять", 5, "кг"),
        ("три штуки", 3, "шт"),
        ("пять коробок", 5, "кор"),
    ],
)
def test_quantity_modal_owns_text_and_voice_replies(
    settings,
    input_kind: InputKind,
    phrase: str,
    expected_quantity: float,
    expected_unit: str,
) -> None:  # type: ignore[no-untyped-def]
    """Маршрутизирует quantity-shaped ответы в текущую позицию."""
    engine = ConversationEngine(settings)
    state = _quantity_state(ItemStatus.MISSING_QTY)
    command = _interpreter().interpret_text(phrase, state)

    result = engine.handle(
        TelegramEvent(
            update_id=1,
            chat_id="modal",
            input_type=input_kind,
            text=phrase,
        ),
        command,
        state,
        [],
    )

    item = result.state.cart[0]
    assert len(result.state.cart) == 1
    assert item.quantity == expected_quantity
    assert item.unit == expected_unit
    if expected_unit == "кг":
        assert item.status is ItemStatus.MATCHED
    else:
        assert item.status is ItemStatus.UNIT_MISMATCH


def test_quantity_modal_rejects_arbitrary_number_sentence(settings) -> None:  # type: ignore[no-untyped-def]
    """Не извлекает число из обычного предложения с товаром."""
    engine = ConversationEngine(settings)
    state = _quantity_state(ItemStatus.UNIT_MISMATCH)
    before = state.cart[0].model_copy(deep=True)
    phrase = "Уточнить один товар"

    result = engine.handle(
        TelegramEvent(update_id=2, chat_id="modal", input_type=InputKind.VOICE, text=phrase),
        _interpreter().interpret_text(phrase, state),
        state,
        [],
    )

    assert result.state.cart[0] == before
    assert len(result.state.cart) == 1
    assert "Товар добавлен в черновик заказа" not in result.reply.text


def test_quantity_modal_does_not_call_history_for_conversational_wrapper(settings) -> None:  # type: ignore[no-untyped-def]
    """Не передаёт разговорный ответ количества в History."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(
        intent=Intent.HISTORY_QUERY,
        text="пусть будет пять",
    )
    interpreter = TelegramInputInterpreter(provider, lambda: Mock(), StateCompatibilityPolicy())
    engine = ConversationEngine(settings)
    state = _quantity_state(ItemStatus.UNIT_MISMATCH)

    command = interpreter.interpret_text("пусть будет пять", state)
    result = engine.handle(
        TelegramEvent(
            update_id=3, chat_id="modal", input_type=InputKind.TEXT, text="пусть будет пять"
        ),
        command,
        state,
        [],
    )

    assert command.intent is Intent.EDIT_QUANTITY
    assert result.state.cart[0].quantity == 5
    provider.parse_text.assert_not_called()


def test_candidate_modal_still_owns_bare_number(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет выбор кандидата, когда карточка кандидатов действительно открыта."""
    state = ConversationState(
        stage=SessionStage.REVIEW,
        current_issue_item_id="ambiguous",
        cart=[
            CartItem(
                id="ambiguous",
                source_query="Сыр",
                status=ItemStatus.AMBIGUOUS,
                candidates=[
                    Candidate(product_id="one", name="Сыр первый"),
                    Candidate(product_id="two", name="Сыр второй"),
                    Candidate(product_id="three", name="Сыр третий"),
                    Candidate(product_id="four", name="Сыр четвёртый"),
                    Candidate(product_id="five", name="Сыр пятый"),
                ],
            )
        ],
    )

    command = _interpreter().interpret_text("5", state)

    assert command.intent is Intent.SELECT_CANDIDATE
    assert command.selected_index == 5


def test_bare_number_without_modal_is_not_authorized_as_candidate() -> None:
    """Не разрешает голому числу создавать выбор без текущей карточки."""
    state = ConversationState(stage=SessionStage.COLLECTING)

    command = _interpreter().interpret_text("5", state)

    assert command.intent is Intent.UNKNOWN


def test_strong_product_command_interrupts_quantity_modal(settings) -> None:  # type: ignore[no-untyped-def]
    """Прерывает quantity modal новой самостоятельной позицией."""
    engine = ConversationEngine(settings)
    state = _quantity_state(ItemStatus.MISSING_QTY)
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="добавь укроп 2 кг",
        items=[ExtractedItem(product_query="укроп", quantity=2, unit="кг")],
        explicit_add_items=True,
    )

    result = engine.handle(
        TelegramEvent(
            update_id=4,
            chat_id="modal",
            input_type=InputKind.TEXT,
            text=command.text,
        ),
        command,
        state,
        [],
    )

    assert len(result.state.cart) == 2
    assert result.state.cart[0].quantity is None
    assert result.state.cart[1].source_query == "укроп"


def test_add_more_keeps_proven_product_when_visible_action_is_absent() -> None:
    """Сохраняет детерминированный новый товар без совпадения с кнопкой."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.UNKNOWN)
    provider.choose_visible_action.return_value = ""
    interpreter = TelegramInputInterpreter(provider, lambda: Mock(), StateCompatibilityPolicy())
    state = ConversationState(
        stage=SessionStage.AWAIT_ADD_MORE_CONFIRM,
        cart=[CartItem(id="old", source_query="Сироп", status=ItemStatus.MATCHED, quantity=1)],
        visible_actions=[{"label": "Добавить ещё товары", "action_id": "v2:add"}],
    )

    command = interpreter.interpret_text("Мука черемуховая", state)

    assert command.intent is Intent.ADD_ITEMS
    assert command.items
    assert command.items[0].product_query == "Мука черемуховая"


def test_spoken_quantity_has_no_arbitrary_number_scan() -> None:
    """Не принимает число из фразы, которая не является количеством."""
    assert PendingQuantityHandler.spoken_quantity("Уточнить один товар") == (None, "")
