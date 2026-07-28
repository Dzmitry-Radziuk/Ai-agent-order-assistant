from restaurant_bot.domain.models import (
    CartItem,
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


def _matched_syrup(
    item_id: str,
    name: str,
    quantity: float,
) -> CartItem:
    """Создаёт сопоставленную позицию сиропа для проверки голосового изменения."""
    return CartItem(
        id=item_id,
        source_query=name,
        quantity=quantity,
        unit="шт",
        catalog_product_id=item_id,
        catalog_name=name,
        catalog_unit="шт",
        status=ItemStatus.MATCHED,
    )


def test_spoken_quantity_edit_selects_named_product_instead_of_first_cart_row(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Меняет названный товар с учётом падежа, не первую строку черновика."""
    engine = ConversationEngine(settings)
    state = ConversationState(
        cart=[
            _matched_syrup("rose", "Сироп Роза, 1л", 5),
            _matched_syrup("sangria", "Сироп Сангрия, 1л", 10),
            _matched_syrup("pistachio", "Сироп Фисташка, 1л", 4),
        ]
    )

    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.EDIT_QUANTITY,
            target_query="сиропа фисташка",
            edit_quantity=7,
            edit_unit="шт",
        ),
        state,
        [],
    )

    assert [item.quantity for item in result.state.cart] == [5, 10, 7]


def test_ambiguous_or_unknown_quantity_target_never_changes_first_cart_row(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не меняет произвольный товар, когда название не найдено однозначно."""
    engine = ConversationEngine(settings)
    state = ConversationState(
        cart=[
            _matched_syrup("rose", "Сироп Роза, 1л", 5),
            _matched_syrup("pistachio", "Сироп Фисташка, 1л", 4),
        ]
    )

    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.EDIT_QUANTITY,
            target_query="сироп манго",
            edit_quantity=7,
            edit_unit="шт",
        ),
        state,
        [],
    )

    assert [item.quantity for item in result.state.cart] == [5, 4]
    assert result.reply.text == "Позиция для изменения не найдена."


def test_spoken_quantity_edit_understands_short_product_case_ending(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Сопоставляет короткое название «сыра» с товаром «Сыр»."""
    engine = ConversationEngine(settings)
    state = ConversationState(cart=[_matched_syrup("cheese", "Сыр Российский", 2)])

    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.EDIT_QUANTITY,
            target_query="сыра",
            edit_quantity=5,
            edit_unit="шт",
        ),
        state,
        [],
    )

    assert result.state.cart[0].quantity == 5
