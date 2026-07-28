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
from restaurant_bot.services.parser import parse_product_lines


def _event(text: str = "") -> TelegramEvent:
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT, text=text)


def _photo_event() -> TelegramEvent:
    """Создаёт событие с фотографией заявки."""
    return TelegramEvent(
        update_id=2,
        chat_id="123456",
        input_type=InputKind.PHOTO,
        file_id="photo-1",
        mime_type="image/jpeg",
    )


def _catalog() -> list[CatalogProduct]:
    """Создаёт тестовый каталог товаров."""
    return [
        CatalogProduct(
            product_id="milk", name="Молоко 3,2%", supplier="Молочный двор", unit="л", price=80
        )
    ]


def test_add_exact_product_to_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что добавление точный товар в черновик."""
    result = ConversationEngine(settings).handle(
        _event("Молоко 3,2% 10 л"),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Молоко 3,2%", quantity=10, unit="л")],
        ),
        ConversationState(),
        _catalog(),
    )
    assert len(result.state.cart) == 1
    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert result.state.cart[0].amount == 800


def test_missing_quantity_starts_clarification(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что отсутствующий количество начинает clarification."""
    result = ConversationEngine(settings).handle(
        _event("Молоко 3,2%"),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=[ExtractedItem(product_query="Молоко 3,2%")]),
        ConversationState(),
        _catalog(),
    )
    assert result.state.cart[0].status is ItemStatus.MISSING_QTY
    assert result.state.current_issue_item_id == result.state.cart[0].id
    assert "Укажите количество" in result.reply.text


def test_exact_long_catalog_name_with_packaging_is_selected_as_one_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Сопоставляет полное название с фасовкой без ложных позиций."""
    name = (
        "ME-ФБ-Глазной мускул говяжий с/м В/У ~ 2кг*10(~20кг) Фермерский бычок Мираторг (Брянск) Ро"
    )
    catalog = [
        CatalogProduct(
            product_id="beef-eye-muscle",
            name=name,
            supplier="Мираторг",
            unit="кг",
        )
    ]

    result = ConversationEngine(settings).handle(
        _event(f"{name} 100 кг"),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query=name, quantity=100, unit="кг")],
        ),
        ConversationState(),
        catalog,
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].catalog_product_id == "beef-eye-muscle"
    assert result.state.cart[0].quantity == 100
    assert result.state.cart[0].status is ItemStatus.MATCHED


def test_catalog_weight_in_full_product_name_is_not_order_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает фасовку из полного названия за количество заказа."""
    name = "Концентрат Интерквас красного сусла, 650г"
    catalog = [
        CatalogProduct(
            product_id="kvass-concentrate",
            name=name,
            supplier="Поставщик",
            unit="шт",
        )
    ]

    result = ConversationEngine(settings).handle(
        _event(name),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=parse_product_lines(name)),
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.catalog_name == name
    assert item.source_line == name
    assert item.quantity is None
    assert item.unit == ""
    assert item.status is ItemStatus.MISSING_QTY
    assert "Укажите количество" in result.reply.text
    assert "Вы указали: <b>650" not in result.reply.text
    assert "Добавить <b>650" not in result.reply.text


def test_quantity_after_full_packaged_name_is_kept_as_order_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Берёт количество только после полного названия вместе с фасовкой."""
    name = "Концентрат Интерквас красного сусла, 650г"
    text = f"{name} 2 шт"
    catalog = [
        CatalogProduct(
            product_id="kvass-concentrate",
            name=name,
            supplier="Поставщик",
            unit="шт",
        )
    ]

    result = ConversationEngine(settings).handle(
        _event(text),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=parse_product_lines(text)),
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.catalog_name == name
    assert item.quantity == 2
    assert item.unit == "шт"
    assert item.status is ItemStatus.MATCHED


def test_shortened_ai_name_matches_packaged_catalog_product(settings) -> None:  # type: ignore[no-untyped-def]
    """Сверяет исходное полное название и сохраняет количество заказа."""
    name = "Сыр Швейцарский Сыробогатов 180гр"
    text = f"{name} 10 штук"
    catalog = [
        CatalogProduct(
            product_id="swiss-cheese",
            name=name,
            supplier="Левина ОА",
            unit="шт",
        )
    ]
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="Сыр Швейцарский Сыробогатов",
                quantity=10,
                unit="шт",
                source_line=text,
            )
        ],
    )

    result = ConversationEngine(settings).handle(
        _event(text),
        command,
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.catalog_name == name
    assert item.supplier == "Левина ОА"
    assert item.quantity == 10
    assert item.unit == "шт"
    assert item.status is ItemStatus.MATCHED


def test_photo_without_order_quantities_does_not_create_draft_items(settings) -> None:  # type: ignore[no-untyped-def]
    """Не создаёт позиции из одной только фасовки на фотографии."""
    result = ConversationEngine(settings).handle(
        _photo_event(),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=[]),
        ConversationState(),
        _catalog(),
    )

    assert result.state.cart == []
    assert "Не нашёл заполненных количеств" in result.reply.text
    assert "Фасовку и справочные значения" in result.reply.text


def test_photo_quantity_survives_catalog_matching(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет количество из колонки заказа после сопоставления с каталогом."""
    catalog = [
        CatalogProduct(
            product_id="rose-syrup",
            name="Сироп Роза, 1л",
            supplier="МБР",
            unit="шт",
        )
    ]
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="Сироп Роза, 1л",
                quantity=5,
                unit="шт",
                department="Кухня",
                supplier_hint="МБР",
                comment="тест",
                source_line=(
                    "Кухня | Сироп Роза, 1л | шт | МБР | | | 5 | тест | р. 1 500 | Пн, Ср | нет"
                ),
                quantity_source="department_columns",
            )
        ],
    )

    result = ConversationEngine(settings).handle(
        _photo_event(),
        command,
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.catalog_product_id == "rose-syrup"
    assert item.quantity == 5
    assert item.unit == "шт"
    assert item.status is ItemStatus.MATCHED
    assert "Укажите количество" not in result.reply.text


def test_handwritten_photo_quantity_survives_catalog_matching(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет рукописное количество свободного списка после поиска в каталоге."""
    catalog = [
        CatalogProduct(
            product_id="chicken",
            name="Курица",
            supplier="Поставщик",
            unit="кг",
        )
    ]
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="Курица",
                quantity=1,
                unit="кг",
                source_line="Курица | 1 кг",
                quantity_source="handwritten",
            )
        ],
    )

    result = ConversationEngine(settings).handle(
        _photo_event(),
        command,
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.quantity == 1
    assert item.unit == "кг"
    assert item.status is ItemStatus.MATCHED


def test_crossed_out_photo_quantity_uses_only_handwritten_replacement(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет рукописную замену и не возвращает зачёркнутое значение."""
    catalog = [
        CatalogProduct(
            product_id="beef",
            name="Говядина вырезка",
            supplier="Поставщик",
            unit="кг",
        )
    ]
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="Говядина вырезка",
                quantity=5,
                unit="кг",
                source_line="Говядина вырезка | зачёркнуто 10 | 5 кг",
                quantity_source="handwritten_correction",
                order_entry_type="handwritten_correction",
            )
        ],
    )

    result = ConversationEngine(settings).handle(
        _photo_event(),
        command,
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.quantity == 5
    assert item.quantity_source == "handwritten_correction"
    assert item.order_entry_type == "handwritten_correction"
    assert item.status is ItemStatus.MATCHED


def test_empty_draft_uses_the_source_n8n_start_card(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что пустой результат черновик использует исходный n8n start карточка."""
    result = ConversationEngine(settings).handle(
        _event(),
        ParsedCommand(intent=Intent.SUBMIT_REQUEST),
        ConversationState(),
        [],
    )

    assert (
        result.reply.text
        == "🧾 <b>Черновик пуст</b>\n\nОтправьте товары текстом, голосом или фото."
    )
    assert [[button.text, button.callback_data] for row in result.reply.rows for button in row] == [
        ["Начать", "v2:add"]
    ]


def test_manual_action_without_an_open_item_uses_source_recovery_card(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что ручной действие без an открытый позиция использует исходный восстановление карточка."""
    result = ConversationEngine(settings).handle(
        _event(),
        ParsedCommand(intent=Intent.MANUAL_CURRENT),
        ConversationState(),
        [],
    )

    assert (
        result.reply.text
        == "ℹ️ <b>Нет товара для изменения</b>\n\nОткройте черновик или добавьте новый товар."
    )
    assert [[button.text, button.callback_data] for row in result.reply.rows for button in row] == [
        ["📦 Показать черновик", "v2:back"],
        ["➕ Добавить еще товары", "v2:add"],
    ]
