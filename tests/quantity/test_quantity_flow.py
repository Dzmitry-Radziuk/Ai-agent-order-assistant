"""Проверяет поведение, связанное с модулем «test quantity flow»."""

from restaurant_bot.domain.models import (
    CartItem,
    CatalogProduct,
    ConversationState,
    DepartmentQuantities,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_callbacks import parse_callback
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
    """Проверяет, что повторение той же отсутствующей позиции не создаёт дубликат."""
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


def test_department_choice_survives_quantity_change_and_supplier_warning(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет выбранный отдел после исправления количества перед предупреждением поставщика."""
    engine = ConversationEngine(settings)
    item = CartItem(
        id="mustard",
        source_query="Горчица",
        catalog_product_id="mustard",
        catalog_name="Горчица",
        catalog_unit="шт",
        unit="шт",
        quantity=4,
        price=100,
        supplier="Тестовый поставщик",
        supplier_minimum_amount=5000,
        minimum_multiple=3,
        suggested_quantity=6,
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(
        cart=[item],
        current_issue_item_id=item.id,
        department_confirmation_required=True,
    )
    department_prompt = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.SUBMIT_REQUEST),
        state,
        [],
    )

    selected = engine.handle(
        TelegramEvent(update_id=2, chat_id="123456", input_type=InputKind.CALLBACK),
        ParsedCommand(intent=Intent.SELECT_DEPARTMENT, callback_target="bar"),
        department_prompt.state,
        [],
    )
    assert "Проверьте количество" in selected.reply.text
    corrected = engine.handle(
        TelegramEvent(
            update_id=3,
            chat_id="123456",
            input_type=InputKind.TEXT,
            text="3 шт",
        ),
        ParsedCommand(intent=Intent.EDIT_QUANTITY, edit_quantity=3, edit_unit="шт"),
        selected.state,
        [],
    )
    reviewed = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.SHOW_FINAL_REVIEW),
        corrected.state,
        [],
    )

    callbacks = [button.callback_data for row in reviewed.reply.rows for button in row]
    assert corrected.state.department_confirmed is True
    assert corrected.state.cart[0].department == "Бар"
    assert corrected.state.cart[0].department_quantities == DepartmentQuantities()
    assert "v2:minsum" in callbacks
    assert "v2:dept:bar" not in callbacks
    assert "Минимальная сумма поставщика" in reviewed.reply.text


def test_accepting_suggested_quantity_keeps_single_department(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет выбранный отдел, когда сотрудник принимает предложенное количество."""
    engine = ConversationEngine(settings)
    item = CartItem(
        id="mustard",
        source_query="Горчица",
        quantity=4,
        suggested_quantity=6,
        department="Бар",
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(
        cart=[item],
        current_issue_item_id=item.id,
        department="Бар",
        department_confirmation_required=True,
        department_confirmed=True,
    )

    accepted = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.ACCEPT_SUGGESTED_QUANTITY),
        state,
        [],
    )

    assert accepted.state.cart[0].quantity == 6
    assert accepted.state.cart[0].department == "Бар"
    assert accepted.state.department_confirmed is True
    assert "v2:dept:bar" not in [
        button.callback_data for row in accepted.reply.rows for button in row
    ]


def test_accepting_photo_quantities_keeps_each_line_department(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет отделы фото при последовательном принятии допустимых количеств."""
    engine = ConversationEngine(settings)
    items = [
        CartItem(
            id="kitchen",
            source_query="Куриное филе",
            quantity=6,
            department="Кухня",
            department_quantities=DepartmentQuantities(kitchen=6),
            status=ItemStatus.MATCHED,
        ),
        CartItem(
            id="hall",
            source_query="Куриное филе",
            quantity=20,
            department="Зал",
            department_quantities=DepartmentQuantities(hall=20),
            minimum_multiple=7,
            suggested_quantity=21,
            status=ItemStatus.MATCHED,
        ),
        CartItem(
            id="bar",
            source_query="Куриное филе",
            quantity=2,
            department="Бар",
            department_quantities=DepartmentQuantities(bar=2),
            minimum_multiple=3,
            suggested_quantity=3,
            status=ItemStatus.MATCHED,
        ),
    ]
    state = ConversationState(
        stage=SessionStage.AWAIT_SUBMIT_CONFIRM,
        cart=items,
        current_issue_item_id="hall",
        department_confirmation_required=True,
        department_confirmed=True,
    )

    accepted_hall = engine.handle(
        TelegramEvent(
            update_id=1,
            chat_id="123456",
            input_type=InputKind.CALLBACK,
            callback_data="v2:accept_multiple:hall",
        ),
        parse_callback("v2:accept_multiple:hall"),
        state,
        [],
    )
    accepted_bar = engine.handle(
        TelegramEvent(
            update_id=2,
            chat_id="123456",
            input_type=InputKind.CALLBACK,
            callback_data="v2:accept_multiple:bar",
        ),
        parse_callback("v2:accept_multiple:bar"),
        accepted_hall.state,
        [],
    )

    assert accepted_hall.state.cart[1].department_quantities == DepartmentQuantities(hall=21)
    assert accepted_bar.state.cart[2].department_quantities == DepartmentQuantities(bar=3)
    assert accepted_bar.state.department_confirmed is True
    assert [engine._submission_department_quantities(item) for item in accepted_bar.state.cart] == [
        [("Кухня", 6)],
        [("Зал", 21)],
        [("Бар", 3)],
    ]
    assert not any(
        button.callback_data.startswith("v2:dept:")
        for row in accepted_bar.reply.rows
        for button in row
    )


def test_quantity_change_reconfirms_preserved_photo_distribution(settings) -> None:  # type: ignore[no-untyped-def]
    """Повторно просит проверить распределение по отделам после изменения количества с фото."""
    engine = ConversationEngine(settings)
    item = CartItem(
        id="mustard",
        source_query="Горчица",
        catalog_product_id="mustard",
        catalog_name="Горчица",
        catalog_unit="шт",
        unit="шт",
        quantity=4,
        department_quantities=DepartmentQuantities(hall=2, kitchen=2),
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(
        cart=[item],
        current_issue_item_id=item.id,
        department_confirmation_required=True,
    )
    selected = engine.handle(
        TelegramEvent(update_id=2, chat_id="123456", input_type=InputKind.CALLBACK),
        ParsedCommand(intent=Intent.SELECT_DEPARTMENT, callback_target="preserve"),
        state,
        [],
    )

    corrected = engine.handle(
        TelegramEvent(
            update_id=3,
            chat_id="123456",
            input_type=InputKind.TEXT,
            text="5 шт",
        ),
        ParsedCommand(intent=Intent.EDIT_QUANTITY, edit_quantity=5, edit_unit="шт"),
        selected.state,
        [],
    )
    reviewed = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.SHOW_FINAL_REVIEW),
        corrected.state,
        [],
    )

    assert corrected.state.department_confirmed is False
    assert corrected.state.cart[0].department_quantities == DepartmentQuantities()
    assert "v2:dept:hall" in [button.callback_data for row in reviewed.reply.rows for button in row]


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


def test_quantity_edit_prefers_complete_name_over_longer_similar_name(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Выбирает полное название, а не более длинную позицию с теми же словами."""
    engine = ConversationEngine(settings)
    state = ConversationState(
        cart=[
            _matched_syrup("dijon", "Горчица дижонская", 3),
            _matched_syrup(
                "whole_grain",
                "Горчица дижонская большое зерно",
                2,
            ),
        ]
    )

    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.EDIT_QUANTITY,
            target_query="горчицы дижонской",
            edit_quantity=10,
            edit_unit="шт",
        ),
        state,
        [],
    )

    assert [item.quantity for item in result.state.cart] == [10, 2]


def test_quantity_edit_keeps_shared_short_name_ambiguous(settings) -> None:  # type: ignore[no-untyped-def]
    """Не выбирает товар, если запрос одинаково относится к похожим позициям."""
    engine = ConversationEngine(settings)
    state = ConversationState(
        cart=[
            _matched_syrup("dijon", "Горчица дижонская", 3),
            _matched_syrup(
                "whole_grain",
                "Горчица дижонская большое зерно",
                2,
            ),
        ]
    )

    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.EDIT_QUANTITY,
            target_query="горчицы",
            edit_quantity=10,
            edit_unit="шт",
        ),
        state,
        [],
    )

    assert [item.quantity for item in result.state.cart] == [3, 2]
    assert "Нашёл несколько похожих позиций" in result.reply.text
    assert "Горчица дижонская" in result.reply.text
    assert "Горчица дижонская большое зерно" in result.reply.text


def test_voice_removal_ignores_draft_location_in_product_name(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаляет названный товар, даже если AI сохранил хвост «из заявки»."""
    engine = ConversationEngine(settings)
    state = ConversationState(
        cart=[
            _matched_syrup("cornmeal", "Крупа кукурузная Алина, 700 г", 5),
            _matched_syrup("rose", "Сироп Роза, 1л", 2),
        ]
    )

    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.REMOVE_ITEM,
            target_query="крупу кукурузную из заявки, пожалуйста",
            target_queries=["крупу кукурузную из заявки, пожалуйста"],
        ),
        state,
        [],
    )

    assert result.state.cart[0].status is ItemStatus.SKIPPED
    assert result.state.cart[1].status is ItemStatus.MATCHED
    assert "Позиция удалена" in result.reply.text


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


def test_multiple_quantity_callback_updates_exact_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Привязывает кнопку кратности к товару, который был показан пользователю."""
    dijon = CartItem(
        id="dijon",
        source_query="Горчица дижонская",
        catalog_product_id="dijon-id",
        catalog_name="Горчица дижонская",
        catalog_unit="шт",
        quantity=1,
        unit="шт",
        minimum_multiple=3,
        suggested_quantity=3,
        status=ItemStatus.MATCHED,
    )
    whole = CartItem(
        id="whole",
        source_query="Горчица дижонская большое зерно",
        catalog_product_id="whole-id",
        catalog_name="Горчица дижонская большое зерно",
        catalog_unit="шт",
        quantity=1,
        unit="шт",
        minimum_multiple=3,
        suggested_quantity=3,
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(
        stage=SessionStage.AWAIT_SUBMIT_CONFIRM,
        current_issue_item_id="whole",
        cart=[dijon, whole],
    )
    command = parse_callback("v2:accept_multiple:dijon")

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=2,
            chat_id="123456",
            input_type=InputKind.CALLBACK,
            callback_data="v2:accept_multiple:dijon",
        ),
        command,
        state,
        [],
    )

    assert result.state.cart[0].quantity == 3
    assert result.state.cart[1].quantity == 1


def test_accept_multiple_then_shows_supplier_minimum_warning(settings) -> None:  # type: ignore[no-untyped-def]
    """После выбора кратности снова считает минимальную сумму поставщика."""
    item = CartItem(
        id="dijon",
        source_query="Горчица дижонская",
        catalog_product_id="dijon-id",
        catalog_name="Горчица дижонская",
        catalog_unit="шт",
        supplier="Тестовый поставщик",
        price=100,
        supplier_minimum_amount=5000,
        supplier_current_sum=0,
        quantity=1,
        unit="шт",
        minimum_multiple=3,
        suggested_quantity=3,
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(
        stage=SessionStage.AWAIT_SUBMIT_CONFIRM,
        current_issue_item_id=item.id,
        cart=[item],
    )

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=3,
            chat_id="123456",
            input_type=InputKind.CALLBACK,
            callback_data="v2:accept_multiple:dijon",
        ),
        parse_callback("v2:accept_multiple:dijon"),
        state,
        [],
    )

    assert result.state.cart[0].quantity == 3
    assert "Минимальная сумма поставщика" in result.reply.text
    assert "300 ₽ из 5000 ₽" in result.reply.text
