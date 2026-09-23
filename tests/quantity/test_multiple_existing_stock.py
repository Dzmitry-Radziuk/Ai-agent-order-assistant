"""Проверяет поведение, связанное с модулем «test multiple existing stock»."""

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
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.presentation.telegram.replies import final_review_reply
from restaurant_bot.services.engine import ConversationEngine


def _event() -> TelegramEvent:
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT)


def test_multiple_recommendation_applies_to_requested_quantity_only(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет кратность количества заявки без прибавления значения из таблицы."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="beef",
            name="Говядина",
            supplier="Мясо",
            unit="кг",
            minimum_multiple=20,
            department_quantities=DepartmentQuantities(kitchen=1),
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
    assert item.existing_quantity == 1
    assert item.suggested_quantity == 20


def test_accepting_multiple_recommendation_changes_only_current_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что принятие кратность recommendation изменяет только текущий позиция."""
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
    """Проверяет, что ручная корректировка кратности сохраняет количество до ввода числа пользователем."""
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


def test_final_review_explains_batch_without_internal_terms(settings) -> None:  # type: ignore[no-untyped-def]
    """Объясняет ограничение понятным пользователю языком."""
    engine = ConversationEngine(settings)
    item = engine._build_item(ExtractedItem(product_query="Глазной мускул", quantity=10, unit="кг"))
    item.status = ItemStatus.MATCHED
    item.catalog_name = "Глазной мускул"
    item.catalog_unit = "кг"
    item.minimum_multiple = 20
    item.suggested_quantity = 20

    reply = final_review_reply(ConversationState(cart=[item]))

    assert "Этот товар можно заказать по <b>20 кг</b>" in reply.text
    assert "20 кг, 40 кг, 60 кг и так далее." in reply.text
    assert "Вы указали: <b>10 кг</b>" in reply.text
    assert "Ближайший подходящий вариант: <b>20 кг</b>" in reply.text
    assert "нужно" not in reply.text.casefold()
    assert "партия" not in reply.text.casefold()
    assert reply.rows[0][0].text == "Выбрать количество"


def test_final_review_suggests_full_multiple_instead_of_missing_difference(settings) -> None:  # type: ignore[no-untyped-def]
    """Предлагает три штуки при кратности три независимо от значения в таблице."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="mustard",
            name="Горчица дижонская большое зерно",
            supplier="МЕТРО",
            unit="шт",
            minimum_multiple=3,
            department_quantities=DepartmentQuantities(kitchen=1),
        )
    ]
    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Горчица дижонская большое зерно",
                    quantity=1,
                    unit="шт",
                )
            ],
        ),
        ConversationState(),
        catalog,
    )

    reply = final_review_reply(result.state)

    assert result.state.cart[0].suggested_quantity == 3
    assert "Этот товар можно заказать по <b>3 шт</b>" in reply.text
    assert "3 шт, 6 шт, 9 шт и так далее." in reply.text
    assert "Ближайший подходящий вариант: <b>3 шт</b>" in reply.text
    assert "Ближайший подходящий вариант: <b>2 шт</b>" not in reply.text


def test_non_multiple_above_first_step_requires_correction(settings) -> None:  # type: ignore[no-untyped-def]
    """Предлагает следующее кратное, если количество выше первого шага."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="mustard",
            name="Горчица дижонская большое зерно",
            supplier="МЕТРО",
            unit="шт",
            minimum_multiple=3,
        )
    ]
    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Горчица дижонская большое зерно",
                    quantity=4,
                    unit="шт",
                )
            ],
        ),
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.quantity == 4
    assert item.suggested_quantity == 6
    assert item.status is ItemStatus.MATCHED


def test_exact_multiple_does_not_require_correction(settings) -> None:  # type: ignore[no-untyped-def]
    """Не предлагает замену для уже допустимого кратного количества."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="mustard",
            name="Горчица дижонская большое зерно",
            supplier="МЕТРО",
            unit="шт",
            minimum_multiple=3,
            department_quantities=DepartmentQuantities(kitchen=1),
        )
    ]
    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Горчица дижонская большое зерно",
                    quantity=6,
                    unit="шт",
                )
            ],
        ),
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.quantity == 6
    assert item.existing_quantity == 1
    assert item.suggested_quantity is None
    assert item.status is ItemStatus.MATCHED


def test_fix_multiple_button_opens_choice_without_changing_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Не меняет количество до явного выбора пользователя."""
    engine = ConversationEngine(settings)
    item = engine._build_item(ExtractedItem(product_query="Говядина", quantity=10, unit="кг"))
    item.id = "beef"
    item.status = ItemStatus.MATCHED
    item.catalog_name = "Говядина"
    item.catalog_unit = "кг"
    item.minimum_multiple = 20
    item.suggested_quantity = 20
    state = ConversationState(cart=[item])

    choice = engine.handle(_event(), parse_callback("v2:mulone"), state, [])

    assert choice.state.cart[0].quantity == 10
    assert choice.state.current_issue_item_id == "beef"
    assert "Выберите количество" in choice.reply.text
    assert [row[0].callback_data for row in choice.reply.rows[:3]] == [
        "v2:accept_multiple:beef",
        "v2:enter_quantity:beef",
        "v2:keep_current:beef",
    ]

    accepted = engine.handle(
        _event(),
        parse_callback("v2:accept_multiple:beef"),
        choice.state,
        [],
    )

    assert accepted.state.cart[0].quantity == 20
    assert "Финальная проверка" in accepted.reply.text


def test_accepted_multiple_quantity_is_written_instead_of_stale_department_value(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """После выбора нового количества заявка не использует старое значение отдела."""
    engine = ConversationEngine(settings)
    item = engine._build_item(ExtractedItem(product_query="Говядина", quantity=5, unit="кг"))
    item.id = "beef"
    item.status = ItemStatus.MATCHED
    item.catalog_product_id = "beef"
    item.catalog_name = "Говядина"
    item.catalog_unit = "кг"
    item.minimum_multiple = 20
    item.department_quantities = DepartmentQuantities(kitchen=1)
    item.suggested_quantity = 20
    state = ConversationState(
        restaurant="Тест",
        spreadsheet_id="venue-sheet",
        current_issue_item_id="beef",
        cart=[item],
    )

    accepted = engine.handle(_event(), parse_callback("v2:accept_multiple"), state, [])
    prepared = engine._prepare_submission(_event(), accepted.state)
    submission_rows = prepared.state.pending_submission.rows  # type: ignore[union-attr]

    assert [(row["_department"], row["Кол-во"]) for row in submission_rows] == [("Кухня", 20)]
