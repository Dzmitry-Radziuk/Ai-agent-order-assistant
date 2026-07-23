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
    return TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT)


def test_convertible_units_are_converted_to_catalog_unit(settings) -> None:  # type: ignore[no-untyped-def]
    result = ConversationEngine(settings).handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сок", quantity=1000, unit="мл")],
        ),
        ConversationState(),
        [CatalogProduct(product_id="juice", name="Сок", supplier="Напитки", unit="л")],
    )

    item = result.state.cart[0]
    assert item.status is ItemStatus.MATCHED
    assert (item.quantity, item.unit, item.catalog_unit) == (1, "л", "л")


def test_incompatible_unit_requires_confirmation_instead_of_silent_change(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    added = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сироп Роза", quantity=10, unit="шт")],
        ),
        ConversationState(),
        [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="л")],
    )

    assert added.state.cart[0].status is ItemStatus.UNIT_MISMATCH
    assert "в заявке этот товар считается" in added.reply.text.lower()

    accepted = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.USE_CATALOG_UNIT, callback_target="0"),
        added.state,
        [],
    )
    assert accepted.state.cart[0].status is ItemStatus.MATCHED
    assert accepted.state.cart[0].unit == "л"
    assert accepted.state.cart[0].quantity == 10


def test_missing_quantity_keeps_catalog_unit_as_the_expected_unit(settings) -> None:  # type: ignore[no-untyped-def]
    result = ConversationEngine(settings).handle(
        _event(),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=[ExtractedItem(product_query="Говядина")]),
        ConversationState(),
        [CatalogProduct(product_id="beef", name="Говядина", supplier="Мясо", unit="кг")],
    )

    item = result.state.cart[0]
    assert item.status is ItemStatus.MISSING_QTY
    assert item.catalog_unit == "кг"
    assert "Укажите количество" in result.reply.text
