"""Проверяет поведение, связанное с модулем «test voice routing contract»."""

from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def test_voice_add_product_phrase_is_not_interpreted_as_candidate_selection() -> None:
    """Проверяет, что голос добавление товар phrase является не interpreted как кандидат выбор."""
    command = infer_intent("добавь сироп роза 10 штук")

    assert command.intent is Intent.ADD_ITEMS
    assert command.selected_index is None
    assert command.items[0].product_query == "сироп роза"
    assert command.items[0].quantity == 10


def test_empty_voice_transcript_cannot_select_a_candidate() -> None:
    """Проверяет, что пустой результат голосовой транскрипции не выбирает кандидата."""
    command = infer_intent("")

    assert command.intent is Intent.UNKNOWN
    assert command.selected_index is None


def test_voice_enumeration_keeps_product_without_shared_quantity() -> None:
    """Проверяет, что голос перечисление сохраняет товар без общая количество."""
    command = infer_intent("сироп и говядина 10 килограмм")

    assert command.intent is Intent.ADD_ITEMS
    assert [(item.product_query, item.quantity, item.unit) for item in command.items] == [
        ("сироп", None, ""),
        ("говядина", 10.0, "кг"),
    ]


def test_voice_add_products_phrases_are_navigation_not_product_lines() -> None:
    """Проверяет, что голос добавление товары phrases являются навигация не товар lines."""
    for phrase in ["добавить товары", "добавь товары", "давай добавим товары"]:
        command = infer_intent(phrase)
        assert command.intent is Intent.ADD_MORE
        assert command.items == []


def test_next_command_removes_previously_polluted_navigation_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Очищает ложный «Тестовый товар», созданный старой маршрутизацией."""
    state = ConversationState(
        cart=[
            CartItem(
                id="bad-command",
                source_query="Давай добавим товаров.",
                catalog_name="Тестовый товар",
                status=ItemStatus.MISSING_QTY,
            )
        ],
        current_issue_item_id="bad-command",
    )
    event = TelegramEvent(
        update_id=1,
        chat_id="1",
        input_type=InputKind.VOICE,
    )

    result = ConversationEngine(settings).handle(
        event,
        ParsedCommand(intent=Intent.ADD_MORE, text="Давай пойдём и добавим товары"),
        state,
        [],
    )

    assert result.state.cart == []
    assert result.state.current_issue_item_id == ""
    assert result.state.stage.value == "collecting"


def test_procurement_request_navigation_can_never_pollute_cart(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаляет старую позицию, созданную из команды просмотра запросов."""
    state = ConversationState(
        cart=[
            CartItem(
                id="bad-request-command",
                source_query="Показать запросы снабженцу.",
                catalog_name="Тестовый товар",
                status=ItemStatus.MISSING_QTY,
            )
        ],
        current_issue_item_id="bad-request-command",
        product_add_requests=[
            {"request_id": "request-1", "description": "Редкий соус", "status": "submitted"}
        ],
    )
    phrase = "Давай посмотрим запросы"

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=2,
            chat_id="1",
            input_type=InputKind.VOICE,
            text=phrase,
        ),
        infer_intent(phrase),
        state,
        [],
    )

    assert result.state.cart == []
    assert result.state.current_issue_item_id == ""
    assert "Запросы снабженцу" in result.reply.text
