"""Проверяет поведение, связанное с модулем «test new order confirmation routing»."""

from __future__ import annotations

import pytest

from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    CatalogProduct,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    PendingSubmission,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def _event(text: str, input_type: InputKind = InputKind.TEXT) -> TelegramEvent:
    """Создаёт событие для confirmation-routing regression tests."""
    return TelegramEvent(
        update_id=912340,
        chat_id="new-order-confirmation",
        input_type=input_type,
        text=text,
    )


def _item(*, status: ItemStatus = ItemStatus.MATCHED, quantity: float | None = 5) -> CartItem:
    """Создаёт позицию текущего черновика."""
    return CartItem(
        id="chicken",
        source_query="курица",
        catalog_name="Курица филе",
        catalog_product_id="chicken-id",
        catalog_unit="кг",
        quantity=quantity,
        unit="кг",
        status=status,
    )


def _state(**kwargs: object) -> ConversationState:
    """Создаёт state с открытым подтверждением новой заявки."""
    values: dict[str, object] = {
        "cart": [_item()],
        "stage": SessionStage.REVIEW,
        "status": "review",
        "pending_new_order_confirmation": True,
        "order_trace_id": "trace-old",
        "ui_revision": 10,
    }
    values.update(kwargs)
    return ConversationState(**values)


def _run(settings, phrase: str, state: ConversationState, kind: InputKind = InputKind.TEXT):
    """Прогоняет текстовую или голосовую команду через engine."""
    return ConversationEngine(settings).handle(
        _event(phrase, kind), infer_intent(phrase), state, []
    )


@pytest.mark.parametrize("phrase", ["да, добавь пармезан 3 кг", "нет, добавь сыр"])
def test_parser_preserves_explicit_product_after_dialogue_marker(phrase: str) -> None:
    """Общий parser не теряет явное добавление после да/нет-маркера."""
    command = infer_intent(phrase)

    assert command.intent is Intent.ADD_ITEMS
    assert command.explicit_add_items is True
    assert command.items[0].product_query in {"пармезан", "сыр"}


def test_parser_keeps_negated_add_as_non_positive_action() -> None:
    """Отрицательная команда не превращается в положительное добавление."""
    command = infer_intent("не добавляй сыр")

    assert command.intent is not Intent.ADD_ITEMS


@pytest.mark.parametrize("kind", [InputKind.TEXT, InputKind.VOICE])
def test_mixed_affirmative_product_interrupts_without_reset(settings, kind: InputKind) -> None:
    """Товар после да не подтверждает удаление старого черновика."""
    state = _state()
    result = _run(settings, "да, добавь пармезан 3 кг", state, kind)

    assert result.state.pending_new_order_confirmation is False
    assert result.state.cart[0].source_query == "курица"
    assert any(item.source_query == "пармезан" for item in result.state.cart)
    assert result.state.cart[0].quantity == 5


def test_product_without_quantity_interrupts_confirmation(settings) -> None:
    """Новая позиция без количества проходит обычный missing-quantity flow."""
    result = _run(settings, "пармезан", _state())

    assert result.state.pending_new_order_confirmation is False
    assert [item.source_query for item in result.state.cart] == ["курица", "пармезан"]
    assert result.state.cart[1].status is ItemStatus.NOT_FOUND


def test_clean_yes_starts_empty_order(settings) -> None:
    """Чистое подтверждение выполняет единственный destructive reset."""
    result = _run(settings, "да", _state())

    assert result.state.cart == []
    assert result.state.pending_new_order_confirmation is False
    assert result.state.stage is SessionStage.COLLECTING


def test_no_preserves_draft_and_resumes_underlying_review(settings) -> None:
    """Отказ закрывает overlay и сохраняет текущий черновик."""
    state = _state()
    result = _run(settings, "нет", state)

    assert result.state.pending_new_order_confirmation is False
    assert result.state.cart == state.cart
    assert result.state.order_trace_id == "trace-old"
    assert result.state.stage is SessionStage.REVIEW


def test_repeated_start_new_order_is_not_yes(settings) -> None:
    """Повторная просьба о новой заявке не подтверждает удаление автоматически."""
    state = _state()
    result = _run(settings, "новый заказ", state)

    assert result.state.pending_new_order_confirmation is True
    assert result.state.cart == state.cart
    assert result.state.order_trace_id == "trace-old"


def test_explicit_clear_is_destructive(settings) -> None:
    """Явная команда очистки остаётся отдельным destructive действием."""
    result = _run(settings, "/reset", _state())

    assert result.state.cart == []
    assert result.state.pending_new_order_confirmation is False


@pytest.mark.parametrize("phrase", ["удали курицу", "измени курицу на 7 кг"])
def test_remove_and_edit_interrupt_without_reset(settings, phrase: str) -> None:
    """Изменение существующей позиции не запускает новую заявку."""
    result = _run(settings, phrase, _state())

    assert result.state.pending_new_order_confirmation is False
    if phrase.startswith("измени"):
        assert result.state.cart[0].quantity == 7
    else:
        assert result.state.cart[0].status is ItemStatus.SKIPPED


def test_edit_comment_interrupts_without_reset(settings) -> None:
    """Изменение комментария проходит обычным current-draft routing."""
    command = infer_intent("добавь комментарий к курице")
    command = command.model_copy(
        update={
            "intent": Intent.EDIT_COMMENT,
            "comment_target_query": "курица",
            "comment_text": "без кожи",
        }
    )
    result = ConversationEngine(settings).handle(
        _event("добавь комментарий к курице"), command, _state(), []
    )

    assert result.state.pending_new_order_confirmation is False
    assert result.state.cart[0].comment == "без кожи"


def test_photo_with_real_items_interrupts_in_current_draft(settings) -> None:
    """Фото с товарами не может подтвердить очистку старой заявки."""
    command = infer_intent("пармезан 3 кг")
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=12,
            chat_id="new-order-confirmation",
            input_type=InputKind.PHOTO,
        ),
        command,
        _state(),
        [],
    )

    assert result.state.pending_new_order_confirmation is False
    assert result.state.cart[0].source_query == "курица"
    assert any(item.source_query == "пармезан" for item in result.state.cart)


def test_photo_without_items_does_not_reset_draft(settings) -> None:
    """Фото без распознанных строк сохраняет текущий черновик."""
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=13,
            chat_id="new-order-confirmation",
            input_type=InputKind.PHOTO,
        ),
        ParsedCommand(intent=Intent.ADD_ITEMS),
        _state(),
        [],
    )

    assert result.state.pending_new_order_confirmation is False
    assert result.state.cart[0].source_query == "курица"


@pytest.mark.parametrize("phrase", ["покажи черновик", "помощь", "спасибо"])
def test_independent_navigation_and_passive_intents_close_overlay(
    settings,
    phrase: str,
) -> None:
    """Независимые команды не проглатываются карточкой подтверждения."""
    result = _run(settings, phrase, _state())

    assert result.state.pending_new_order_confirmation is False
    assert result.state.cart[0].source_query == "курица"


def test_unknown_repeats_confirmation_without_mutation(settings) -> None:
    """Неуверенная фраза оставляет draft и confirmation без изменений."""
    state = _state()
    result = _run(settings, "ну", state)

    assert result.state.pending_new_order_confirmation is True
    assert result.state.cart == state.cart
    assert "Начать новую заявку?" in result.reply.text


def test_quantity_answer_resumes_underlying_modal(settings) -> None:
    """Количество после overlay применяется к исходной нерешённой позиции."""
    item = _item(status=ItemStatus.MISSING_QTY, quantity=None)
    state = _state(
        cart=[item],
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
        status="await_unit_quantity",
        current_issue_item_id=item.id,
    )
    result = _run(settings, "5 кг", state)

    assert result.state.pending_new_order_confirmation is False
    assert result.state.cart[0].quantity == 5
    assert result.state.cart[0].status is ItemStatus.MATCHED


def test_candidate_callback_answer_resumes_underlying_modal(settings) -> None:
    """Выбор кандидата после overlay продолжает старый candidate flow."""
    item = _item(status=ItemStatus.AMBIGUOUS, quantity=None)
    item.candidates = [
        Candidate(product_id="p1", name="Сыр Пармезан", unit="кг"),
        Candidate(product_id="p2", name="Сыр Гауда", unit="кг"),
    ]
    state = _state(
        cart=[item],
        current_issue_item_id=item.id,
        current_issue_kind="candidate",
    )
    result = ConversationEngine(settings).handle(
        _event("второй"),
        infer_intent("второй"),
        state,
        [CatalogProduct(product_id="p2", name="Сыр Гауда", unit="кг")],
    )

    assert result.state.pending_new_order_confirmation is False
    assert result.state.cart[0].catalog_product_id == "p2"


def test_stale_new_order_callback_cannot_reset_draft(settings) -> None:
    """Устаревший callback отклоняется до confirmation transition."""
    state = _state()
    before = state.model_dump_json()
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=9,
            chat_id="new-order-confirmation",
            input_type=InputKind.CALLBACK,
            callback_data="v2:clear:r9",
        ),
        parse_callback("v2:clear:r9"),
        state,
        [],
    )

    assert result.state.model_dump_json() == before
    assert result.state.pending_new_order_confirmation is True


def test_submitting_confirmation_cannot_destroy_frozen_submission(settings) -> None:
    """Активная отправка блокирует reset новой заявки."""
    pending = PendingSubmission(order_no="ORDER-1", rows=[{"Количество": 5}])
    state = _state(
        stage=SessionStage.SUBMITTING,
        status="submitting",
        pending_submission=pending,
    )
    result = _run(settings, "да", state)

    assert result.state.stage is SessionStage.SUBMITTING
    assert result.state.pending_submission == pending
    assert result.state.cart[0].source_query == "курица"
    assert "уже обрабатывается" in result.reply.text


def test_fresh_clear_callback_starts_new_order(settings) -> None:
    """Свежая кнопка YES использует существующий callback contract."""
    state = _state()
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=10,
            chat_id="new-order-confirmation",
            input_type=InputKind.CALLBACK,
            callback_data="v2:clear:r10",
        ),
        parse_callback("v2:clear:r10"),
        state,
        [],
    )

    assert result.state.cart == []
    assert result.state.pending_new_order_confirmation is False


def test_fresh_back_callback_resumes_draft(settings) -> None:
    """Свежая кнопка NO закрывает overlay без destructive reset."""
    state = _state()
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=11,
            chat_id="new-order-confirmation",
            input_type=InputKind.CALLBACK,
            callback_data="v2:back:r10",
        ),
        parse_callback("v2:back:r10"),
        state,
        [],
    )

    assert result.state.pending_new_order_confirmation is False
    assert result.state.cart == state.cart
