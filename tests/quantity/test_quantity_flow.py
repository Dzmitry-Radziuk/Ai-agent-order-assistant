from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine


def _event() -> TelegramEvent:
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT)


def test_quantity_answer_completes_current_missing_item_without_new_cart_line(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что количество ответ завершает текущий отсутствующий позиция без новый черновик строка."""
    engine = ConversationEngine(settings)
    catalog = [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")]
    pending = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=[ExtractedItem(product_query="Сироп Роза")]),
        ConversationState(),
        catalog,
    )

    completed = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.EDIT_QUANTITY, edit_quantity=10, edit_unit="шт"),
        pending.state,
        catalog,
    )

    assert len(completed.state.cart) == 1
    assert completed.state.cart[0].status is ItemStatus.MATCHED
    assert (completed.state.cart[0].quantity, completed.state.cart[0].unit) == (10, "шт")


def test_repeating_same_missing_item_does_not_create_duplicate(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что повторение тот же отсутствующий позиция выполняет не create дубликат."""
    engine = ConversationEngine(settings)
    catalog = [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")]
    first = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=[ExtractedItem(product_query="Сироп Роза")]),
        ConversationState(),
        catalog,
    )

    repeated = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=[ExtractedItem(product_query="Сироп Роза")]),
        first.state,
        catalog,
    )

    assert len(repeated.state.cart) == 1
    assert repeated.state.cart[0].status is ItemStatus.MISSING_QTY
