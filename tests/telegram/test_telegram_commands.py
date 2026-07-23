import pytest

from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.parser import infer_intent


@pytest.mark.parametrize(
    ("command", "intent"),
    [
        ("/start", Intent.GREETING),
        ("/help", Intent.HELP),
        ("/draft", Intent.SHOW_CART),
        ("/reset", Intent.CLEAR_CART),
        ("/submit", Intent.SUBMIT_REQUEST),
        ("/orders", Intent.ORDER_STATUS),
        ("/cancel", Intent.CANCEL),
    ],
)
def test_primary_telegram_commands_have_deterministic_intents(command: str, intent: Intent) -> None:
    assert infer_intent(command).intent is intent


def test_command_with_bot_suffix_has_same_intent() -> None:
    assert infer_intent("/submit@restaurant_order_bot").intent is Intent.SUBMIT_REQUEST


def test_draft_and_cancel_preserve_existing_procurement_request(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    state = ConversationState(
        cart=[
            CartItem(
                id="rose",
                source_query="Сироп Роза",
                quantity=5,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
        ],
        product_add_requests=[
            {"request_id": "add-1", "description": "Креветки Polar", "status": "submitted"}
        ],
    )
    event = TelegramEvent(update_id=1, chat_id="1", input_type=InputKind.TEXT)

    draft = engine.handle(event, infer_intent("/draft"), state, [])
    cancelled = engine.handle(event, infer_intent("/cancel"), draft.state, [])

    assert "Сироп Роза" in draft.reply.text
    assert "Запросы снабженцу: 1" in draft.reply.text
    assert any(
        button.callback_data == "v2:addreqlist" for row in draft.reply.rows for button in row
    )
    assert cancelled.state.cart[0].status is ItemStatus.MATCHED
    assert cancelled.state.product_add_requests[0]["request_id"] == "add-1"


def test_order_status_callback_uses_source_progress_text(settings) -> None:  # type: ignore[no-untyped-def]
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=1, chat_id="1", input_type=InputKind.CALLBACK, callback_message_id=42
        ),
        infer_intent("", "v2:orders:r3"),
        ConversationState(ui_revision=3),
        [],
    )

    assert result.enqueue_order_status is True
    assert result.reply.text == "Обновляю статус заявки..."
