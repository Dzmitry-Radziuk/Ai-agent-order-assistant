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


def test_selected_supplier_is_strict_filter_and_other_supplier_is_only_a_suggestion(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что выбранный поставщик является строгий filter и другое поставщик является только a предложение."""
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


def test_short_catalog_supplier_matches_selected_full_supplier_name(settings) -> None:  # type: ignore[no-untyped-def]
    """Считает полное и сокращённое названия одним поставщиком."""
    name = "Филейный край говяжий с/м В/У ~3,5кг Травяной откорм Мираторг (Брянск) РОССИЯ"
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=2,
            chat_id="123456",
            input_type=InputKind.TEXT,
            text=name,
        ),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Филейный край говяжий с/м В/У",
                    quantity=3.5,
                    unit="кг",
                    comment="Травяной откорм",
                    source_line=name,
                )
            ],
        ),
        ConversationState(
            supplier_hint_context="Мираторг (Брянск) РОССИЯ",
        ),
        [
            CatalogProduct(
                product_id="beef-fillet",
                name=name,
                supplier="Мираторг",
                unit="кг",
            )
        ],
    )

    item = result.state.cart[0]
    assert item.catalog_product_id == "beef-fillet"
    assert item.supplier == "Мираторг"
    assert item.supplier_search_locked is False
    assert item.status is ItemStatus.MISSING_QTY
    assert item.quantity is None
    assert item.source_query == "Филейный край говяжий с/м В/У"
    assert item.source_line == name
    assert item.comment == ""
    assert "Укажите количество" in result.reply.text


def test_supplier_choice_scopes_only_the_next_product_message(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что поставщик выбор scopes только следующий товар сообщение."""
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
    """Проверяет, что поиск все suppliers повторяет тот же позиция без дубликат."""
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
