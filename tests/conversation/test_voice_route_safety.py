"""Проверяет поведение, связанное с модулем «test voice route safety»."""

import pytest

from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    ConversationState,
    InputKind,
    ItemStatus,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def _event(phrase: str, input_kind: InputKind = InputKind.VOICE) -> TelegramEvent:
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(
        update_id=1,
        chat_id="123456",
        input_type=input_kind,
        text=phrase,
    )


def _item(status: ItemStatus) -> CartItem:
    """Создаёт тестовую позицию заявки."""
    return CartItem(
        id="cheese",
        source_query="Сыр Швейцарский Сыробогатов 180гр",
        catalog_product_id="cheese-product",
        catalog_name="Сыр Швейцарский Сыробогатов 180гр",
        quantity=100,
        unit="кг",
        catalog_unit="шт",
        status=status,
    )


@pytest.mark.parametrize("input_kind", [InputKind.TEXT, InputKind.VOICE])
@pytest.mark.parametrize(
    "phrase",
    [
        "Не добавлять",
        "Не добавляй этот товар",
        "Этот товар не нужен",
        "Убери эту позицию",
        "Пропусти и иди дальше",
        "Пропускаем",
        "Давай откажемся от этого товара",
    ],
)
def test_negative_item_phrases_skip_unit_mismatch_without_confirming_quantity(
    settings,
    input_kind: InputKind,
    phrase: str,
) -> None:
    """Не превращает отказ от товара в подтверждение единицы каталога."""
    item = _item(ItemStatus.UNIT_MISMATCH)
    state = ConversationState(
        stage=SessionStage.COLLECTING,
        cart=[item],
        current_issue_item_id=item.id,
    )

    result = ConversationEngine(settings).handle(
        _event(phrase, input_kind),
        infer_intent(phrase),
        state,
        [],
    )

    assert result.state.cart[0].status is ItemStatus.SKIPPED
    assert result.state.cart[0].unit == "кг"
    assert result.state.cart[0].quantity == 100
    assert "Товар добавлен" not in result.reply.text


@pytest.mark.parametrize(
    "phrase",
    [
        "Не отправляй заявку",
        "Пожалуйста, не подтверждай отправку",
        "Я передумал, не надо отправлять",
        "Отмена, верни к черновику",
    ],
)
def test_negative_submit_phrases_never_submit_order(settings, phrase: str) -> None:  # type: ignore[no-untyped-def]
    """Отрицание отправки возвращает к черновику без фоновой отправки."""
    item = _item(ItemStatus.MATCHED)
    item.unit = "шт"
    state = ConversationState(
        stage=SessionStage.AWAIT_SUBMIT_CONFIRM,
        cart=[item],
    )

    result = ConversationEngine(settings).handle(
        _event(phrase),
        infer_intent(phrase),
        state,
        [],
    )

    assert result.enqueue_submission is False
    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert result.state.stage is not SessionStage.SUBMITTING


def test_negative_product_add_phrase_cancels_pending_procurement_request(settings) -> None:  # type: ignore[no-untyped-def]
    """Не отправляет запрос снабженцу при явном голосовом отказе."""
    item = _item(ItemStatus.NOT_FOUND)
    item.catalog_product_id = ""
    item.catalog_name = ""
    item.catalog_unit = ""
    state = ConversationState(
        stage=SessionStage.AWAIT_PRODUCT_ADD_DETAILS,
        cart=[item],
        current_issue_item_id=item.id,
        pending_product_add_item_index=0,
        pending_product_add_request_id="request-1",
    )
    phrase = "Нет, не отправляй этот запрос снабженцу"

    result = ConversationEngine(settings).handle(
        _event(phrase),
        infer_intent(phrase),
        state,
        [],
    )

    assert result.enqueue_product_add is False
    assert result.state.cart[0].status is ItemStatus.SKIPPED
    assert result.state.pending_product_add_request_id == ""


def test_negated_candidate_number_does_not_select_it(settings) -> None:  # type: ignore[no-untyped-def]
    """Фраза «не первый» не выбирает первый вариант из-за упомянутого номера."""
    item = _item(ItemStatus.AMBIGUOUS)
    item.catalog_product_id = ""
    item.catalog_name = ""
    item.catalog_unit = ""
    item.candidates = [
        Candidate(product_id="one", name="Сыр первый", score=0.9),
        Candidate(product_id="two", name="Сыр второй", score=0.8),
    ]
    state = ConversationState(
        stage=SessionStage.COLLECTING,
        cart=[item],
        current_issue_item_id=item.id,
    )
    phrase = "Не выбирай первый вариант"

    result = ConversationEngine(settings).handle(
        _event(phrase),
        infer_intent(phrase),
        state,
        [],
    )

    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert result.state.cart[0].catalog_product_id == ""
    assert "несколько вариантов" in result.reply.text


@pytest.mark.parametrize(
    "phrase",
    ["Не объединяй", "Не добавляй повторно", "Оставь как есть", "Пропусти повтор"],
)
def test_negative_duplicate_phrases_never_merge_quantities(settings, phrase: str) -> None:  # type: ignore[no-untyped-def]
    """Не увеличивает количество существующего товара при отказе от дубля."""
    existing = _item(ItemStatus.MATCHED)
    existing.id = "existing"
    existing.quantity = 2
    existing.unit = "шт"
    duplicate = _item(ItemStatus.DUPLICATE_PENDING)
    duplicate.id = "duplicate"
    duplicate.quantity = 3
    duplicate.unit = "шт"
    duplicate.issue_message = existing.id
    state = ConversationState(
        cart=[existing, duplicate],
        current_issue_item_id=duplicate.id,
    )

    result = ConversationEngine(settings).handle(
        _event(phrase),
        infer_intent(phrase),
        state,
        [],
    )

    assert result.state.cart[0].quantity == 2
    assert result.state.cart[1].status is ItemStatus.SKIPPED


def test_negative_add_more_phrase_keeps_last_added_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Отказ продолжать завершает вопрос, но не удаляет уже добавленный товар."""
    item = _item(ItemStatus.MATCHED)
    item.unit = "шт"
    state = ConversationState(
        stage=SessionStage.AWAIT_ADD_MORE_CONFIRM,
        cart=[item],
    )
    phrase = "Я больше не хочу добавлять товары"

    result = ConversationEngine(settings).handle(
        _event(phrase),
        infer_intent(phrase),
        state,
        [],
    )

    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert result.state.stage is SessionStage.REVIEW
    assert "Черновик заявки" in result.reply.text
