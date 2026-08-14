"""Проверяет поведение, связанное с модулем «test new order flow»."""

import pytest

from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def _event(text: str, input_type: InputKind = InputKind.VOICE) -> TelegramEvent:
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(
        update_id=701572470,
        chat_id="chat-1",
        input_type=input_type,
        text=text,
        telegram_user_id="user-1",
    )


def _matched_item() -> CartItem:
    """Создаёт сопоставленную позицию тестовой заявки."""
    return CartItem(
        id="rose",
        source_query="Сироп роза",
        catalog_name="Сироп Роза, 1л",
        quantity=10,
        unit="шт",
        status=ItemStatus.MATCHED,
    )


@pytest.mark.parametrize(
    ("phrase", "input_type"),
    [
        ("Новый заказ.", InputKind.VOICE),
        ("Хочу оформить ещё одну заявку", InputKind.TEXT),
    ],
)
def test_new_order_after_submission_starts_empty_draft_and_preserves_history(
    settings,
    phrase: str,
    input_type: InputKind,
) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что после отправки новый заказ начинает пустой черновик и сохраняет историю."""
    state = ConversationState(
        stage=SessionStage.SUBMITTED,
        status="submitted",
        cart=[
            CartItem(
                id="old-navigation",
                source_query="Новый заказ.",
                status=ItemStatus.SKIPPED,
            )
        ],
        last_order_no="20260727-182324-1661",
        submitted_order_numbers=["20260727-182324-1661"],
        restaurant="Кафе",
        telegram_user_id="user-1",
        telegram_chat_id="chat-1",
        venue_code="cafe",
        venue_name="Кафе",
        spreadsheet_id="sheet-1",
        spreadsheet_url="https://docs.google.test/sheet-1",
        role="Повар",
        metadata={"onboarding_shown": True, "registration": "kept"},
        department="Кухня",
        product_add_requests=[
            {
                "request_id": "request-1",
                "description": "Редкий соус",
                "status": "submitted",
            }
        ],
    )

    command = infer_intent(phrase)
    result = ConversationEngine(settings).handle(
        _event(phrase, input_type),
        command,
        state,
        [],
    )

    assert command.intent is Intent.START_NEW_ORDER
    assert result.state.stage is SessionStage.COLLECTING
    assert result.state.status == "collecting"
    assert result.state.cart == []
    assert result.state.pending_new_order_confirmation is False
    assert result.state.last_order_no == "20260727-182324-1661"
    assert result.state.submitted_order_numbers == ["20260727-182324-1661"]
    assert result.state.spreadsheet_id == "sheet-1"
    assert result.state.venue_code == "cafe"
    assert result.state.metadata["registration"] == "kept"
    assert result.state.product_add_requests[0]["request_id"] == "request-1"
    assert result.reply.text == (
        "🧾 <b><u>Новая заявка</u></b>\n\n"
        "Отправьте товары текстом, голосом или фото — я добавлю их в текущий черновик заказа.\n\n"
        "Можно отправить один товар, список или фото заполненной таблицы. "
        "Я распознаю названия, количество и комментарии.\n\n"
        "Когда закончите, скажите «покажи итог» — я покажу заявку для проверки."
    )


def test_new_order_with_active_draft_requires_confirmation(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что новый заказ при активном черновике требует подтверждения."""
    state = ConversationState(
        stage=SessionStage.REVIEW,
        status="review",
        cart=[_matched_item()],
        order_trace_id="trace-1",
        last_order_no="previous-order",
    )
    phrase = "Создай новый заказ"

    result = ConversationEngine(settings).handle(
        _event(phrase),
        infer_intent(phrase),
        state,
        [],
    )

    assert result.state.cart == [_matched_item()]
    assert result.state.order_trace_id == "trace-1"
    assert result.state.pending_new_order_confirmation is True
    assert "Начать новую заявку?" in result.reply.text
    assert "1 позиция" in result.reply.text
    assert [(button.text, button.callback_data) for row in result.reply.rows for button in row] == [
        ("Да, начать новую", "v2:clear"),
        ("Нет, оставить черновик", "v2:back"),
    ]


def test_spoken_confirmation_clears_active_draft_but_keeps_order_history(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что произнесённый подтверждение очищает активный черновик but сохраняет заказ история."""
    state = ConversationState(
        stage=SessionStage.REVIEW,
        status="review",
        cart=[_matched_item()],
        order_trace_id="trace-1",
        pending_new_order_confirmation=True,
        last_order_no="previous-order",
        submitted_order_numbers=["previous-order"],
        spreadsheet_id="sheet-1",
    )

    result = ConversationEngine(settings).handle(
        _event("Да, начинай новую"),
        infer_intent("Да, начинай новую"),
        state,
        [],
    )

    assert result.state.cart == []
    assert result.state.order_trace_id == ""
    assert result.state.pending_new_order_confirmation is False
    assert result.state.last_order_no == "previous-order"
    assert result.state.submitted_order_numbers == ["previous-order"]
    assert result.state.spreadsheet_id == "sheet-1"
    assert "Новая заявка" in result.reply.text


def test_rejecting_new_order_keeps_active_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что отказ новый заказ сохраняет активный черновик."""
    item = _matched_item()
    state = ConversationState(
        stage=SessionStage.REVIEW,
        status="review",
        cart=[item],
        order_trace_id="trace-1",
        pending_new_order_confirmation=True,
    )

    result = ConversationEngine(settings).handle(
        _event("Нет, оставь черновик"),
        infer_intent("Нет, оставь черновик"),
        state,
        [],
    )

    assert result.state.cart == [item]
    assert result.state.order_trace_id == "trace-1"
    assert result.state.pending_new_order_confirmation is False
    assert "Черновик заявки" in result.reply.text
    assert "Сироп Роза, 1л" in result.reply.text


def test_success_card_new_order_callback_preserves_tracking(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что успех карточка новый заказ callback сохраняет tracking."""
    state = ConversationState(
        stage=SessionStage.SUBMITTED,
        status="submitted",
        last_order_no="ORDER-1",
        submitted_order_numbers=["ORDER-1"],
        spreadsheet_id="sheet-1",
    )

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=2,
            chat_id="chat-1",
            input_type=InputKind.CALLBACK,
            callback_data="v2:clear",
        ),
        parse_callback("v2:clear"),
        state,
        [],
    )

    assert result.state.stage is SessionStage.COLLECTING
    assert result.state.last_order_no == "ORDER-1"
    assert result.state.submitted_order_numbers == ["ORDER-1"]
    assert result.state.spreadsheet_id == "sheet-1"
