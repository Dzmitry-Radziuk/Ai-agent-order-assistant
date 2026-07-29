from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine


def _event() -> TelegramEvent:
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT)


def test_submit_request_opens_final_review_without_enqueuing(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что отправка запрос открывает финальный review без enqueuing."""
    engine = ConversationEngine(settings)
    catalog = [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")]
    added = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Сироп Роза", quantity=5, unit="шт")],
        ),
        ConversationState(),
        catalog,
    )

    review = engine.handle(
        _event(), ParsedCommand(intent=Intent.SUBMIT_REQUEST), added.state, catalog
    )

    assert review.state.stage is SessionStage.AWAIT_SUBMIT_CONFIRM
    assert review.enqueue_submission is False
    assert "Финальная проверка" in review.reply.text
    assert [(button.text, button.callback_data) for row in review.reply.rows for button in row] == [
        ("Отправить в таблицу заказа", "v2:submit"),
        ("К черновику", "v2:back"),
    ]


def test_clear_cart_removes_draft_and_returns_to_collecting(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что очистка черновик удаляет черновик и возвращает в collecting."""
    engine = ConversationEngine(settings)
    state = ConversationState(cart=[])

    cleared = engine.handle(_event(), ParsedCommand(intent=Intent.CLEAR_CART), state, [])

    assert cleared.state.stage is SessionStage.COLLECTING
    assert cleared.state.cart == []
