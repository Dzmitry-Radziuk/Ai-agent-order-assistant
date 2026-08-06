import pytest

from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.parser import parse_callback


def _event() -> TelegramEvent:
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT)


def test_convertible_units_require_explicit_catalog_unit_confirmation(settings) -> None:  # type: ignore[no-untyped-def]
    """Не меняет названную пользователем единицу без явного подтверждения."""
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
    assert item.status is ItemStatus.UNIT_MISMATCH
    assert (item.quantity, item.unit, item.catalog_unit) == (1000, "мл", "л")
    assert "Этот товар заказывается" in result.reply.text


def test_incompatible_unit_requires_confirmation_instead_of_silent_change(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что несовместимая единица измерения требует подтверждение вместо для silent изменение."""
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
    assert "этот товар заказывается" in added.reply.text.lower()

    accepted = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.USE_CATALOG_UNIT, callback_target="0"),
        added.state,
        [],
    )
    assert accepted.state.cart[0].status is ItemStatus.MATCHED
    assert accepted.state.cart[0].unit == "л"
    assert accepted.state.cart[0].quantity == 10


def test_enter_quantity_button_never_adds_zero_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Открывает ввод количества вместо добавления товара с нулём."""
    engine = ConversationEngine(settings)
    name = (
        "МЕ-ФБ-Глазной мускул говяжий с/м В/У ~ 2кг*10(~20кг) Фермерский бычок Мираторг (Брянск) Ро"
    )
    added = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query=name, quantity=10, unit="л")],
        ),
        ConversationState(),
        [CatalogProduct(product_id="beef-eye", name=name, supplier="Мираторг", unit="кг")],
    )

    result = engine.handle(
        TelegramEvent(
            update_id=2,
            chat_id="123456",
            input_type=InputKind.CALLBACK,
            callback_data="v2:unitedit:0",
        ),
        parse_callback("v2:unitedit:0"),
        added.state,
        [],
    )

    item = result.state.cart[0]
    assert result.state.stage is SessionStage.AWAIT_UNIT_QUANTITY
    assert item.status is ItemStatus.UNIT_MISMATCH
    assert item.quantity == 10
    assert item.unit == "л"
    assert "Укажите количество в кг" in result.reply.text
    assert "Товар добавлен" not in result.reply.text


def test_missing_quantity_keeps_catalog_unit_as_the_expected_unit(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что отсутствующий количество сохраняет каталог единица измерения как expected единица измерения."""
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


def test_packaging_in_name_and_order_weight_are_kept_separate(settings) -> None:  # type: ignore[no-untyped-def]
    """Не превращает граммы заказа в штуки товара."""
    name = "Сыр Швейцарский Сыробогатов 180гр"
    text = f"{name} 500 грам свежего"
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=2,
            chat_id="123456",
            input_type=InputKind.TEXT,
            text=text,
        ),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query=name,
                    comment="свежего",
                    source_line=text,
                )
            ],
        ),
        ConversationState(),
        [
            CatalogProduct(
                product_id="swiss-cheese",
                name=name,
                supplier="Сыробогатов",
                unit="шт",
            )
        ],
    )

    item = result.state.cart[0]
    assert item.catalog_name == name
    assert item.quantity == 500
    assert item.unit == "г"
    assert item.catalog_unit == "шт"
    assert item.status is ItemStatus.UNIT_MISMATCH
    assert item.comment == "свежего"
    assert "500 <b>шт" not in result.reply.text
    assert "сколько шт нужно" in result.reply.text


def test_manual_unit_correction_uses_catalog_unit_and_rejects_repeated_weight(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не завершает уточнение, пока пользователь снова вводит граммы."""
    engine = ConversationEngine(settings)
    item = engine._build_item(
        ExtractedItem(
            product_query="Сыр Швейцарский Сыробогатов 180гр",
            quantity=100,
            unit="г",
        )
    )
    item.id = "cheese"
    item.catalog_product_id = "swiss-cheese"
    item.catalog_name = item.source_query
    item.catalog_unit = "шт"
    item.status = ItemStatus.UNIT_MISMATCH
    state = ConversationState(current_issue_item_id=item.id, cart=[item])

    prompt = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.ENTER_OTHER_QUANTITY),
        state,
        [],
    )

    assert prompt.state.stage.value == "await_unit_quantity"
    assert "количество в шт" in prompt.reply.text
    assert "5 шт" in prompt.reply.text
    assert "кг" not in prompt.reply.text
    assert [row[0].text for row in prompt.reply.rows] == [
        "Вернуться к вариантам",
        "Не добавлять",
    ]

    repeated_weight = engine.handle(
        TelegramEvent(
            update_id=2,
            chat_id="123456",
            input_type=InputKind.TEXT,
            text="500 гр",
        ),
        ParsedCommand(intent=Intent.UNKNOWN, text="500 гр"),
        prompt.state,
        [],
    )

    corrected = repeated_weight.state.cart[0]
    assert corrected.status is ItemStatus.UNIT_MISMATCH
    assert corrected.quantity == 500
    assert corrected.unit == "г"
    assert "500 <b>шт" not in repeated_weight.reply.text
    assert "сколько шт нужно" in repeated_weight.reply.text


def test_package_suggestion_button_applies_selected_piece_count(settings) -> None:  # type: ignore[no-untyped-def]
    """Применяет расчёт упаковок только после нажатия пользователя."""
    engine = ConversationEngine(settings)
    item = engine._build_item(ExtractedItem(product_query="Сыр 180гр", quantity=500, unit="г"))
    item.id = "cheese"
    item.catalog_product_id = "cheese"
    item.catalog_name = "Сыр 180гр"
    item.catalog_unit = "шт"
    item.status = ItemStatus.UNIT_MISMATCH
    state = ConversationState(current_issue_item_id=item.id, cart=[item])

    result = engine.handle(
        TelegramEvent(
            update_id=3,
            chat_id="123456",
            input_type=InputKind.CALLBACK,
            callback_data="v2:qty:cheese:3",
        ),
        parse_callback("v2:qty:cheese:3"),
        state,
        [],
    )

    corrected = result.state.cart[0]
    assert corrected.status is ItemStatus.MATCHED
    assert corrected.quantity == 3
    assert corrected.unit == "шт"
    assert "3 шт" in result.reply.text


def test_manual_unit_correction_accepts_quantity_in_catalog_unit(settings) -> None:  # type: ignore[no-untyped-def]
    """Завершает уточнение после количества в единицах каталога."""
    engine = ConversationEngine(settings)
    item = engine._build_item(ExtractedItem(product_query="Сыр 180гр", quantity=100, unit="г"))
    item.id = "cheese"
    item.catalog_product_id = "cheese"
    item.catalog_name = "Сыр 180гр"
    item.catalog_unit = "шт"
    item.status = ItemStatus.UNIT_MISMATCH
    state = ConversationState(
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
        current_issue_item_id=item.id,
        cart=[item],
    )

    result = engine.handle(
        TelegramEvent(
            update_id=3,
            chat_id="123456",
            input_type=InputKind.TEXT,
            text="3 шт",
        ),
        ParsedCommand(intent=Intent.UNKNOWN, text="3 шт"),
        state,
        [],
    )

    corrected = result.state.cart[0]
    assert corrected.status is ItemStatus.MATCHED
    assert corrected.quantity == 3
    assert corrected.unit == "шт"
    assert "3 шт" in result.reply.text


@pytest.mark.parametrize("input_type", [InputKind.TEXT, InputKind.VOICE])
@pytest.mark.parametrize(
    "phrase",
    [
        "добавь три штуки",
        "давай 3 штуки",
        "измени на 3 штуки",
        "пусть будет три",
        "закажи три",
        "выбираю три штуки",
    ],
)
def test_natural_quantity_phrases_apply_to_open_unit_card(
    settings,
    input_type: InputKind,
    phrase: str,
) -> None:  # type: ignore[no-untyped-def]
    """Применяет живую речь только к открытому товару."""
    engine = ConversationEngine(settings)
    item = engine._build_item(ExtractedItem(product_query="Сыр 180гр", quantity=500, unit="г"))
    item.id = "cheese"
    item.catalog_product_id = "cheese"
    item.catalog_name = "Сыр 180гр"
    item.catalog_unit = "шт"
    item.status = ItemStatus.UNIT_MISMATCH
    state = ConversationState(
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
        current_issue_item_id=item.id,
        cart=[item],
    )

    result = engine.handle(
        TelegramEvent(
            update_id=4,
            chat_id="123456",
            input_type=input_type,
            text=phrase,
        ),
        ParsedCommand(intent=Intent.UNKNOWN, text=phrase),
        state,
        [],
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert result.state.cart[0].quantity == 3
    assert result.state.cart[0].unit == "шт"
    assert "3 шт" in result.reply.text
