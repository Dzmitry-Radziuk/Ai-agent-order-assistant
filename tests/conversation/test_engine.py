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


def _event(text: str = "") -> TelegramEvent:
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
    return [
        CatalogProduct(
            product_id="milk", name="Молоко 3,2%", supplier="Молочный двор", unit="л", price=80
        )
    ]


def test_add_exact_product_to_draft(settings) -> None:  # type: ignore[no-untyped-def]
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
    result = ConversationEngine(settings).handle(
        _event("Молоко 3,2%"),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=[ExtractedItem(product_query="Молоко 3,2%")]),
        ConversationState(),
        _catalog(),
    )
    assert result.state.cart[0].status is ItemStatus.MISSING_QTY
    assert result.state.current_issue_item_id == result.state.cart[0].id
    assert "Укажите количество" in result.reply.text


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


def test_empty_draft_uses_the_source_n8n_start_card(settings) -> None:  # type: ignore[no-untyped-def]
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
        ["➕ Начать", "v2:add"]
    ]


def test_manual_action_without_an_open_item_uses_source_recovery_card(settings) -> None:  # type: ignore[no-untyped-def]
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
