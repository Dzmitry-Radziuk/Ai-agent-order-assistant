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
    assert "Товар уже в черновике" in duplicate.reply.text
    assert "Вы добавляете: 3 шт" in duplicate.reply.text


def test_duplicate_merge_adds_only_the_confirmed_increment(settings) -> None:  # type: ignore[no-untyped-def]
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
    assert "Сироп Роза" in merged.reply.text


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
