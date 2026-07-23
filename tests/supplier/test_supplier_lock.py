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


def test_selected_supplier_is_strict_filter_and_other_supplier_is_only_a_suggestion(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="tarragon-a", name="Сироп Тархун", supplier="Поставщик А", unit="шт"
        ),
        CatalogProduct(product_id="rose-b", name="Сироп Роза", supplier="Поставщик Б", unit="шт"),
    ]
    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сироп Роза", quantity=5, unit="шт", supplier_hint="Поставщик А"
                )
            ],
        ),
        ConversationState(supplier_hint_context="Поставщик А"),
        catalog,
    )

    item = result.state.cart[0]
    assert item.status is ItemStatus.NOT_FOUND
    assert item.supplier_search_locked is True
    assert [candidate.product_id for candidate in item.candidates] == ["rose-b"]
    assert "Товар не найден у выбранного поставщика" in result.reply.text
    assert result.state.supplier_hint_context == ""


def test_supplier_choice_scopes_only_the_next_product_message(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="milk-a", name="Молоко", supplier="Поставщик А", unit="шт"),
        CatalogProduct(product_id="milk-b", name="Молоко", supplier="Поставщик Б", unit="шт"),
    ]
    state = ConversationState(supplier_hint_context="Поставщик А")

    first = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Молоко", quantity=1, unit="шт")],
        ),
        state,
        catalog,
    )
    second = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Молоко", quantity=1, unit="шт")],
        ),
        first.state,
        catalog,
    )

    assert first.state.cart[0].supplier == "Поставщик А"
    assert first.state.supplier_hint_context == ""
    assert second.state.cart[1].status is ItemStatus.AMBIGUOUS
    assert {candidate.supplier for candidate in second.state.cart[1].candidates} == {
        "Поставщик А",
        "Поставщик Б",
    }


def test_search_all_suppliers_retries_same_item_without_duplicate(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="tarragon-a", name="Сироп Тархун", supplier="Поставщик А", unit="шт"
        ),
        CatalogProduct(product_id="rose-b", name="Сироп Роза", supplier="Поставщик Б", unit="шт"),
    ]
    locked = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сироп Роза", quantity=5, unit="шт", supplier_hint="Поставщик А"
                )
            ],
        ),
        ConversationState(supplier_hint_context="Поставщик А"),
        catalog,
    )

    retried = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.SEARCH_ALL_SUPPLIERS, callback_target="0"),
        locked.state,
        catalog,
    )

    assert len(retried.state.cart) == 1
    assert retried.state.cart[0].status is ItemStatus.MATCHED
    assert retried.state.cart[0].supplier == "Поставщик Б"
    assert retried.state.cart[0].supplier_search_locked is False
