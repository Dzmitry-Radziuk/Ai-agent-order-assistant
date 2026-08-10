import pytest

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
from restaurant_bot.services.parser import infer_intent


def _event() -> TelegramEvent:
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT)


def _voice(text: str) -> TelegramEvent:
    """Создаёт голосовое событие для проверки команды."""
    return TelegramEvent(
        update_id=2,
        chat_id="123456",
        input_type=InputKind.VOICE,
        text=text,
    )


def test_repeated_product_with_quantity_requires_explicit_merge(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что повторный товар with количество требует явный merge."""
    engine = ConversationEngine(settings)
    catalog = [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")]
    first = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сироп Роза", quantity=5, unit="шт")],
        ),
        ConversationState(),
        catalog,
    )
    duplicate = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сироп Роза", quantity=3, unit="шт")],
        ),
        first.state,
        catalog,
    )

    assert [item.status for item in duplicate.state.cart] == [
        ItemStatus.MATCHED,
        ItemStatus.DUPLICATE_PENDING,
    ]
    assert "Товар уже есть в черновике" in duplicate.reply.text
    assert "Вы добавляете: 3 шт" in duplicate.reply.text


def test_repeated_product_with_other_unit_keeps_duplicate_warning(settings) -> None:  # type: ignore[no-untyped-def]
    """Не объединяет повторный товар, если единица в голосовой фразе другая."""
    engine = ConversationEngine(settings)
    catalog = [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")]
    first = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сироп Роза", quantity=5, unit="шт")],
        ),
        ConversationState(),
        catalog,
    )

    duplicate = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сироп Роза", quantity=2, unit="бан")],
        ),
        first.state,
        catalog,
    )

    assert duplicate.state.cart[1].status is ItemStatus.DUPLICATE_PENDING
    assert "Товар уже есть в черновике" in duplicate.reply.text
    assert "2 бан" in duplicate.reply.text
    assert "в шт" in duplicate.reply.text
    assert "После добавления" not in duplicate.reply.text


def test_remove_and_edit_ignore_skipped_rows_with_same_product(settings) -> None:  # type: ignore[no-untyped-def]
    """Команды изменения и удаления выбирают активную строку, а не старый дубль."""
    engine = ConversationEngine(settings)
    active = engine._build_item(ExtractedItem(product_query="Филе форели", quantity=5, unit="кг"))
    active.id = "active"
    active.catalog_product_id = "trout"
    active.catalog_name = "Филе форели"
    active.catalog_unit = "кг"
    active.status = ItemStatus.MATCHED
    skipped = active.model_copy(deep=True)
    skipped.id = "skipped"
    skipped.status = ItemStatus.SKIPPED
    state = ConversationState(cart=[active, skipped])

    removed = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.REMOVE_ITEM, target_query="филе форели"),
        state,
        [],
    )
    assert removed.state.cart[0].status is ItemStatus.SKIPPED
    assert removed.state.cart[1].status is ItemStatus.SKIPPED
    assert "Позиция удалена" in removed.reply.text

    active.status = ItemStatus.MATCHED
    edited = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.EDIT_QUANTITY,
            target_query="филе форели",
            edit_quantity=2,
            edit_unit="кг",
        ),
        state,
        [],
    )
    assert edited.state.cart[0].quantity == 2
    assert edited.state.cart[1].status is ItemStatus.SKIPPED


def test_duplicate_merge_adds_only_the_confirmed_increment(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что дубликат merge добавляет только confirmed увеличение."""
    engine = ConversationEngine(settings)
    catalog = [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")]
    state = ConversationState()
    first = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сироп Роза", quantity=5, unit="шт")],
        ),
        state,
        catalog,
    )
    duplicate = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сироп Роза", quantity=3, unit="шт")],
        ),
        first.state,
        catalog,
    )

    merged = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.MERGE_DUPLICATE, callback_target="1"),
        duplicate.state,
        catalog,
    )

    assert merged.state.cart[0].quantity == 8
    assert merged.state.cart[1].status is ItemStatus.SKIPPED
    assert "Товар добавлен в черновик заказа" in merged.reply.text
    assert "Сироп Роза" not in merged.reply.text


@pytest.mark.parametrize(
    "phrase",
    [
        "Добавить",
        "Добавляй",
        "Да, добавляй",
        "Давай объединим",
        "Прибавь к текущему",
        "Сложи вместе",
        "Плюсуй",
        "Суммируй",
        "Пусть будет вместе",
        "Окей",
        "Конечно",
    ],
)
def test_voice_variants_merge_duplicate(settings, phrase: str) -> None:  # type: ignore[no-untyped-def]
    """Объединяет повторный товар по разным разговорным подтверждениям."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="rose",
            name="Сироп Роза",
            supplier="Сиропы",
            unit="шт",
        )
    ]
    first = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сироп Роза", quantity=10, unit="шт")],
        ),
        ConversationState(),
        catalog,
    )
    duplicate = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сироп Роза", quantity=10, unit="шт")],
        ),
        first.state,
        catalog,
    )

    merged = engine.handle(
        _voice(phrase),
        infer_intent(phrase),
        duplicate.state,
        catalog,
    )

    assert merged.state.cart[0].quantity == 20
    assert merged.state.cart[1].status is ItemStatus.SKIPPED


@pytest.mark.parametrize(
    "phrase",
    ["Нет", "Не добавлять", "Не надо добавлять", "Пропусти", "Оставь как есть"],
)
def test_voice_variants_skip_duplicate(settings, phrase: str) -> None:  # type: ignore[no-untyped-def]
    """Не объединяет повторный товар по разговорному отказу."""
    engine = ConversationEngine(settings)
    existing = engine._build_item(ExtractedItem(product_query="Сироп Роза", quantity=10, unit="шт"))
    existing.id = "existing"
    existing.status = ItemStatus.MATCHED
    duplicate = engine._build_item(
        ExtractedItem(product_query="Сироп Роза", quantity=10, unit="шт")
    )
    duplicate.status = ItemStatus.DUPLICATE_PENDING
    duplicate.issue_message = existing.id
    state = ConversationState(
        cart=[existing, duplicate],
        current_issue_item_id=duplicate.id,
    )

    skipped = engine.handle(
        _voice(phrase),
        infer_intent(phrase),
        state,
        [],
    )

    assert skipped.state.cart[0].quantity == 10
    assert skipped.state.cart[1].status is ItemStatus.SKIPPED


def test_identical_resolved_voice_duplicates_are_collapsed_without_doubling(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что одинаковые resolved голос дубликаты являются объединяются без doubling."""
    engine = ConversationEngine(settings)
    first = engine._build_item(
        ExtractedItem(product_query="говядина мраморная", quantity=10, unit="кг")
    )
    second = engine._build_item(
        ExtractedItem(product_query="холодным и говядина", quantity=10, unit="кг")
    )
    for item in (first, second):
        item.catalog_product_id = "beef-thin"
        item.catalog_name = "Говядина Тонкий край"
        item.catalog_unit = "кг"
        item.status = ItemStatus.MATCHED
    first.comment = "только мраморная"
    second.comment = "только мраморная"
    state = ConversationState(cart=[first, second])

    engine._remove_exact_cart_duplicates(state)

    assert len(state.cart) == 1
    assert state.cart[0].quantity == 10
    assert state.cart[0].comment == "только мраморная"


def test_resolved_same_product_quantities_are_combined_into_one_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Объединяет ранее сохранённые строки одного товара с разным количеством."""
    engine = ConversationEngine(settings)
    first = engine._build_item(
        ExtractedItem(product_query="Сыр Швейцарский", quantity=2, unit="шт")
    )
    second = engine._build_item(
        ExtractedItem(product_query="Сыр Швейцарский", quantity=3, unit="шт")
    )
    for item in (first, second):
        item.catalog_product_id = "cheese"
        item.catalog_name = "Сыр Швейцарский Сыробогатов 180гр"
        item.catalog_unit = "шт"
        item.status = ItemStatus.MATCHED
    first.comment = "без замены"
    second.comment = "для кухни"
    state = ConversationState(cart=[first, second])

    engine._remove_exact_cart_duplicates(state)

    assert len(state.cart) == 1
    assert state.cart[0].quantity == 5
    assert state.cart[0].comment == "без замены; для кухни"


@pytest.mark.parametrize(
    ("input_kind", "phrase"),
    [
        (InputKind.VOICE, "три."),
        (InputKind.TEXT, "три"),
        (InputKind.VOICE, "добавь три штуки."),
        (InputKind.VOICE, "давай 3 штуки"),
    ],
)
def test_contextual_duplicate_quantity_merges_with_existing_row(
    settings,
    input_kind: InputKind,
    phrase: str,
) -> None:  # type: ignore[no-untyped-def]
    """Прибавляет короткое количество к существующей строке товара."""
    engine = ConversationEngine(settings)
    existing = engine._build_item(
        ExtractedItem(product_query="Сыр Швейцарский", quantity=2, unit="шт")
    )
    existing.id = "existing"
    existing.catalog_product_id = "cheese"
    existing.catalog_name = "Сыр Швейцарский Сыробогатов 180гр"
    existing.catalog_unit = "шт"
    existing.status = ItemStatus.MATCHED
    duplicate = engine._build_item(ExtractedItem(product_query="Сыр Швейцарский"))
    duplicate.id = "duplicate"
    duplicate.catalog_product_id = "cheese"
    duplicate.catalog_name = existing.catalog_name
    duplicate.catalog_unit = "шт"
    duplicate.status = ItemStatus.DUPLICATE_PENDING
    duplicate.issue_message = existing.id
    duplicate.duplicate_existing_quantity = 2
    duplicate.duplicate_existing_unit = "шт"
    state = ConversationState(
        cart=[existing, duplicate],
        current_issue_item_id=duplicate.id,
    )
    event = TelegramEvent(
        update_id=3,
        chat_id="123456",
        input_type=input_kind,
        text=phrase,
    )

    result = engine.handle(event, infer_intent(phrase), state, [])

    assert len(result.state.cart) == 2
    assert result.state.cart[0].quantity == 5
    assert result.state.cart[1].quantity == 3
    assert result.state.cart[1].status is ItemStatus.SKIPPED
    assert result.state.current_issue_item_id == ""
    assert sum(item.status is ItemStatus.MATCHED for item in result.state.cart) == 1
    assert "Сыр Швейцарский Сыробогатов 180гр — 5 шт" in result.reply.text
