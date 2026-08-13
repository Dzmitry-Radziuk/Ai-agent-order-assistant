"""Проверяет поведение, связанное с модулем «test telegram commands»."""

import pytest

from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    TelegramEvent,
)
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.parsing.commands.api import enrich_command, infer_intent
from restaurant_bot.services.engine import ConversationEngine


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
    """Проверяет, что основная модель Telegram команды имеют детерминированный intents."""
    assert infer_intent(command).intent is intent


def test_command_with_bot_suffix_has_same_intent() -> None:
    """Проверяет, что команда с суффиксом имени бота сохраняет то же намерение."""
    assert infer_intent("/submit@restaurant_order_bot").intent is Intent.SUBMIT_REQUEST


def test_draft_and_cancel_preserve_existing_procurement_request(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что черновик и cancel сохраняет существующий procurement запрос."""
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
    """Проверяет, что заказ статус callback использует исходный прогресс текст."""
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=1, chat_id="1", input_type=InputKind.CALLBACK, callback_message_id=42
        ),
        enrich_command("", parse_callback("v2:orders:r3")),
        ConversationState(ui_revision=3),
        [],
    )

    assert result.enqueue_order_status is True
    assert result.reply.text == "Обновляю статус заявки..."


def test_order_status_callback_opens_selected_order(settings) -> None:  # type: ignore[no-untyped-def]
    """Передаёт номер выбранной кнопкой заявки в фоновую задачу."""
    state = ConversationState(
        order_status_view_active=True,
        order_status_order_numbers=["A-1", "A-2"],
    )

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=2,
            chat_id="1",
            input_type=InputKind.CALLBACK,
            callback_data="v2:order:2",
        ),
        enrich_command("", parse_callback("v2:order:2")),
        state,
        [],
    )

    assert result.enqueue_order_status is True
    assert result.order_status_selected_index == 2
    assert result.order_status_page == 0


def test_voice_can_open_order_by_spoken_position(settings) -> None:  # type: ignore[no-untyped-def]
    """Понимает короткое «вторая» на экране списка заявок."""
    state = ConversationState(
        order_status_view_active=True,
        order_status_order_numbers=["A-1", "A-2", "A-3"],
    )
    event = TelegramEvent(
        update_id=3,
        chat_id="1",
        input_type=InputKind.VOICE,
        text="вторая",
    )

    result = ConversationEngine(settings).handle(
        event,
        infer_intent(event.text),
        state,
        [],
    )

    assert result.enqueue_order_status is True
    assert result.order_status_selected_index == 2


def test_voice_can_open_next_order_status_page(settings) -> None:  # type: ignore[no-untyped-def]
    """Понимает «следующие» относительно открытого списка заявок."""
    state = ConversationState(
        order_status_view_active=True,
        order_status_page=0,
        order_status_order_numbers=["A-1", "A-2"],
    )
    event = TelegramEvent(
        update_id=4,
        chat_id="1",
        input_type=InputKind.VOICE,
        text="следующие",
    )

    result = ConversationEngine(settings).handle(
        event,
        infer_intent(event.text),
        state,
        [],
    )

    assert result.enqueue_order_status is True
    assert result.order_status_page == 1


@pytest.mark.parametrize(
    ("phrase", "cart_page", "expected_page"),
    [
        ("следующая страница", 0, 1),
        ("перейди на предыдущую страницу", 1, 0),
    ],
)
def test_voice_pagination_stays_in_large_draft(
    settings, phrase: str, cart_page: int, expected_page: int
) -> None:  # type: ignore[no-untyped-def]
    """Переключает страницы большого черновика, а не открывает историю заявок."""
    state = ConversationState(
        cart_page=cart_page,
        visible_actions=[
            {"label": "← Назад", "action_id": "v2:cartpage:0:r1"},
            {"label": "Далее →", "action_id": "v2:cartpage:1:r1"},
        ],
        cart=[
            CartItem(
                id=f"item-{index}",
                source_query=f"Товар {index}",
                quantity=1,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
            for index in range(21)
        ],
    )
    event = TelegramEvent(
        update_id=5,
        chat_id="1",
        input_type=InputKind.VOICE,
        text=phrase,
    )

    result = ConversationEngine(settings).handle(event, infer_intent(phrase), state, [])

    assert result.enqueue_order_status is False
    assert result.state.cart_page == expected_page
    assert f"Страница {expected_page + 1} из 2" in result.reply.text


@pytest.mark.parametrize(
    ("phrase", "final_review_page", "expected_page"),
    [
        ("следующая страница", 0, 1),
        ("перейди на предыдущую страницу", 1, 0),
    ],
)
def test_voice_pagination_stays_in_large_final_review(
    settings, phrase: str, final_review_page: int, expected_page: int
) -> None:  # type: ignore[no-untyped-def]
    """Переключает страницы финальной проверки, а не открывает историю заявок."""
    state = ConversationState(
        final_review_page=final_review_page,
        visible_actions=[
            {"label": "← Назад", "action_id": "v2:finalpage:0:r1"},
            {"label": "Далее →", "action_id": "v2:finalpage:1:r1"},
        ],
        cart=[
            CartItem(
                id=f"item-{index}",
                source_query=f"Товар {index}",
                quantity=1,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
            for index in range(21)
        ],
    )
    event = TelegramEvent(
        update_id=6,
        chat_id="1",
        input_type=InputKind.VOICE,
        text=phrase,
    )

    result = ConversationEngine(settings).handle(event, infer_intent(phrase), state, [])

    assert result.enqueue_order_status is False
    assert result.state.final_review_page == expected_page
    assert f"Страница {expected_page + 1} из 2" in result.reply.text


def test_voice_short_new_order_transcription_does_not_become_product(settings) -> None:  # type: ignore[no-untyped-def]
    """Маршрутизирует обрезанную голосовую фразу «Новая» в новую заявку."""
    state = ConversationState()
    event = TelegramEvent(
        update_id=7,
        chat_id="1",
        input_type=InputKind.VOICE,
        text="Новая",
    )

    result = ConversationEngine(settings).handle(event, infer_intent(event.text), state, [])

    assert result.state.stage.value == "collecting"
    assert result.state.cart == []
    assert "Новая заявка" in result.reply.text


def test_natural_order_status_phrase_contains_selected_position() -> None:
    """Извлекает позицию из полной человеческой фразы."""
    command = infer_intent("Давай посмотрим вторую заявку")

    assert command.intent is Intent.ORDER_STATUS
    assert command.selected_index == 2


def test_order_status_page_callback_contains_page_number() -> None:
    """Извлекает страницу из безопасного callback без номера заявки."""
    command = enrich_command("", parse_callback("v2:orderspage:3"))

    assert command.intent is Intent.ORDER_STATUS
    assert command.callback_target == "page:3"


@pytest.mark.parametrize(
    ("phrase", "selected_index"),
    [
        ("вторая", 2),
        ("покажи вторую", 2),
        ("открой заявку номер три", 3),
        ("выбери 4", 4),
        ("давай первую", 1),
    ],
)
def test_order_status_voice_selection_accepts_natural_phrases(
    settings,
    phrase: str,
    selected_index: int,
) -> None:  # type: ignore[no-untyped-def]
    """Выбирает заявку разными короткими голосовыми фразами."""
    state = ConversationState(
        order_status_view_active=True,
        order_status_order_numbers=["A-1", "A-2", "A-3", "A-4", "A-5"],
    )
    event = TelegramEvent(
        update_id=5,
        chat_id="1",
        input_type=InputKind.VOICE,
        text=phrase,
    )

    result = ConversationEngine(settings).handle(
        event,
        infer_intent(phrase),
        state,
        [],
    )

    assert result.enqueue_order_status is True
    assert result.order_status_selected_index == selected_index


@pytest.mark.parametrize(
    "phrase",
    [
        "следующие",
        "покажи следующие заявки",
        "покажи более старые",
        "следующая страница",
        "покажи мне следующую страницу",
        "вперёд",
    ],
)
def test_order_status_voice_navigation_accepts_next_page_phrases(
    settings,
    phrase: str,
) -> None:  # type: ignore[no-untyped-def]
    """Открывает более старые заявки разными голосовыми фразами."""
    state = ConversationState(
        order_status_view_active=True,
        order_status_page=1,
        order_status_order_numbers=["A-6", "A-7"],
    )
    event = TelegramEvent(
        update_id=6,
        chat_id="1",
        input_type=InputKind.VOICE,
        text=phrase,
    )

    result = ConversationEngine(settings).handle(
        event,
        infer_intent(phrase),
        state,
        [],
    )

    assert result.enqueue_order_status is True
    assert result.order_status_page == 2


@pytest.mark.parametrize(
    "phrase",
    [
        "предыдущая страница",
        "покажи более новые",
        "назад",
        "листай назад",
        "вернись назад",
    ],
)
def test_order_status_voice_navigation_accepts_previous_page_phrases(
    settings,
    phrase: str,
) -> None:  # type: ignore[no-untyped-def]
    """Возвращает список к более новым заявкам по разговорной фразе."""
    state = ConversationState(
        order_status_view_active=True,
        order_status_page=2,
        order_status_order_numbers=["A-11", "A-12"],
    )
    event = TelegramEvent(
        update_id=7,
        chat_id="1",
        input_type=InputKind.VOICE,
        text=phrase,
    )

    result = ConversationEngine(settings).handle(
        event,
        infer_intent(phrase),
        state,
        [],
    )

    assert result.enqueue_order_status is True
    assert result.order_status_page == 1


def test_order_status_voice_navigation_moves_inside_selected_order(settings) -> None:  # type: ignore[no-untyped-def]
    """Понимает следующую страницу внутри уже открытой заявки."""
    state = ConversationState(
        order_status_view_active=True,
        order_status_detail_active=True,
        order_status_page=0,
        order_status_detail_page=1,
        order_status_selected_index=2,
        order_status_selected_order_number="A-2",
        order_status_order_numbers=["A-1", "A-2"],
    )
    event = TelegramEvent(
        update_id=8,
        chat_id="1",
        input_type=InputKind.VOICE,
        text="предыдущая страница",
    )

    result = ConversationEngine(settings).handle(
        event,
        infer_intent(event.text),
        state,
        [],
    )

    assert result.enqueue_order_status is True
    assert result.order_status_detail_page == 0
    assert result.order_status_selected_index == 2
