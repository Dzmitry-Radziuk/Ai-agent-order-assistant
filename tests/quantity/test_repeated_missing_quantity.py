"""Проверяет поведение, связанное с модулем «test repeated missing quantity»."""

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


def test_repeat_missing_product_keeps_one_open_question(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что repeat отсутствующий товар сохраняет один открытый question."""
    engine = ConversationEngine(settings)
    catalog = [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")]
    first = engine.handle(
        TelegramEvent(update_id=1, chat_id="1", input_type=InputKind.TEXT),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=[ExtractedItem(product_query="Сироп Роза")]),
        ConversationState(),
        catalog,
    )
    second = engine.handle(
        TelegramEvent(update_id=2, chat_id="1", input_type=InputKind.TEXT),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=[ExtractedItem(product_query="Сироп Роза")]),
        first.state,
        catalog,
    )

    assert len(second.state.cart) == 1
    assert second.state.cart[0].status is ItemStatus.MISSING_QTY
