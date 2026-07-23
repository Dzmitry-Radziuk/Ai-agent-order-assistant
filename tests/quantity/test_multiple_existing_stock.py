from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    DepartmentQuantities,
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


def test_multiple_recommendation_accounts_for_existing_department_stock(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="beef",
            name="Говядина",
            supplier="Мясо",
            unit="кг",
            minimum_multiple=20,
            department_quantities=DepartmentQuantities(kitchen=20),
        )
    ]
    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Говядина", quantity=5, unit="кг")],
        ),
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.status is ItemStatus.MATCHED
    assert item.existing_quantity == 20
    assert item.suggested_quantity == 20


def test_accepting_multiple_recommendation_changes_only_current_item(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="beef",
            name="Говядина",
            supplier="Мясо",
            unit="кг",
            minimum_multiple=20,
            department_quantities=DepartmentQuantities(kitchen=20),
        )
    ]
    added = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Говядина", quantity=5, unit="кг")],
        ),
        ConversationState(),
        catalog,
    )
    added.state.current_issue_item_id = added.state.cart[0].id

    accepted = engine.handle(
        _event(), ParsedCommand(intent=Intent.ACCEPT_SUGGESTED_QUANTITY), added.state, catalog
    )

    assert accepted.state.cart[0].quantity == 20
    assert accepted.state.cart[0].status is ItemStatus.MATCHED


def test_manual_multiple_correction_keeps_quantity_until_user_enters_a_number(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    state = ConversationState(
        current_issue_item_id="beef",
        cart=[
            engine._build_item(ExtractedItem(product_query="Говядина", quantity=5, unit="кг")),
            engine._build_item(ExtractedItem(product_query="Сироп", quantity=3, unit="шт")),
        ],
    )
    state.cart[0].id = "beef"
    state.cart[0].status = ItemStatus.MATCHED
    state.cart[0].suggested_quantity = 20
    state.cart[1].status = ItemStatus.MATCHED

    result = engine.handle(_event(), ParsedCommand(intent=Intent.ENTER_OTHER_QUANTITY), state, [])

    assert result.state.stage.value == "await_multiple_quantity"
    assert result.state.cart[0].quantity == 5
    assert result.state.cart[1].quantity == 3
    assert "Укажите другое количество" in result.reply.text
