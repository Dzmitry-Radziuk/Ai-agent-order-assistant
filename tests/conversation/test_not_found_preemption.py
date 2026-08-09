"""Регрессионные сценарии прерывания открытой карточки NOT_FOUND."""

from __future__ import annotations

import pytest

from restaurant_bot.domain.models import (
    CartItem,
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.services.conversation_handlers.state_compatibility import (
    CompatibilityAction,
    CompatibilityContext,
    StateCompatibilityPolicy,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.parser import infer_intent


def _event(text: str, input_type: InputKind = InputKind.TEXT) -> TelegramEvent:
    """Создаёт нормализованное событие для теста NOT_FOUND."""
    return TelegramEvent(update_id=1, chat_id="not-found", input_type=input_type, text=text)


def _not_found_state(stage: SessionStage = SessionStage.COLLECTING) -> ConversationState:
    """Создаёт карточку ненайденного манго с данными старой позиции."""
    item = CartItem(
        id="mango",
        source_query="манго",
        status=ItemStatus.NOT_FOUND,
        quantity=2,
        unit="кг",
        comment="спелое",
        catalog_product_id="old-catalog-id",
        catalog_name="Старое манго",
        catalog_unit="кг",
        product_add_request_id="old-request",
    )
    return ConversationState(
        cart=[item],
        current_issue_item_id=item.id,
        current_issue_kind=None,
        stage=stage,
    )


def _catalog() -> list[CatalogProduct]:
    """Возвращает каталог независимых новых товаров."""
    return [
        CatalogProduct(product_id="parmesan", name="Пармезан", unit="кг"),
        CatalogProduct(product_id="dill", name="Укроп", unit="кг"),
    ]


def _add_command(text: str, query: str, quantity: float = 3) -> ParsedCommand:
    """Создаёт конкретную команду добавления нового товара."""
    return ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text=text,
        items=[ExtractedItem(product_query=query, quantity=quantity, unit="кг")],
    )


def test_not_found_policy_allows_existing_manual_clarification() -> None:
    """Продолжает только уже открытый режим изменения названия товара."""
    state = _not_found_state(SessionStage.AWAIT_MANUAL_DETAILS)
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="манго тайское",
        items=[ExtractedItem(product_query="манго тайское")],
    )

    decision = StateCompatibilityPolicy().evaluate(
        command,
        state,
        CompatibilityContext.NOT_FOUND,
    )

    assert decision.action is CompatibilityAction.CONTINUE


@pytest.mark.parametrize("input_type", [InputKind.TEXT, InputKind.VOICE])
def test_concrete_new_product_interrupts_not_found_without_data_leak(
    settings, input_type: InputKind
) -> None:  # type: ignore[no-untyped-def]
    """Добавляет новый товар отдельно и сохраняет старую NOT_FOUND позицию."""
    state = _not_found_state()
    text = "пармезан три килограмма" if input_type is InputKind.VOICE else "пармезан 3 кг"
    result = ConversationEngine(settings).handle(
        _event(text, input_type),
        _add_command(text, "пармезан"),
        state,
        _catalog(),
    )

    old, new = result.state.cart
    assert old.status is ItemStatus.NOT_FOUND
    assert (old.source_query, old.quantity, old.unit, old.comment) == (
        "манго",
        2,
        "кг",
        "спелое",
    )
    assert (old.catalog_product_id, old.product_add_request_id) == (
        "old-catalog-id",
        "old-request",
    )
    assert (new.source_query, new.quantity, new.unit) == ("пармезан", 3, "кг")
    assert new.comment == ""
    assert new.catalog_product_id != old.catalog_product_id
    assert new.product_add_request_id == ""


def test_not_found_new_product_keeps_current_issue_behavior(settings) -> None:  # type: ignore[no-untyped-def]
    """Фиксирует, какой unresolved item получает фокус после добавления."""
    state = _not_found_state()
    result = ConversationEngine(settings).handle(
        _event("пармезан 3 кг"),
        _add_command("пармезан 3 кг", "пармезан"),
        state,
        _catalog(),
    )

    assert len(result.state.cart) == 2
    assert result.state.current_issue_item_id == "mango"
    assert result.state.stage is SessionStage.COLLECTING


def test_not_found_independent_actions_do_not_mutate_context(settings) -> None:  # type: ignore[no-untyped-def]
    """Навигация и благодарность не применяются к старой карточке товара."""
    for text, command in (
        ("покажи черновик", infer_intent("покажи черновик")),
        ("спасибо", infer_intent("спасибо")),
    ):
        state = _not_found_state()
        result = ConversationEngine(settings).handle(_event(text), command, state, _catalog())

        item = result.state.cart[0]
        assert item.status is ItemStatus.NOT_FOUND
        assert item.source_query == "манго"
        assert item.comment == "спелое"
        assert result.state.current_issue_item_id == "mango"


def test_remove_current_not_found_clears_issue_references(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаление текущего NOT_FOUND товара не оставляет битую активную ссылку."""
    state = _not_found_state()
    result = ConversationEngine(settings).handle(
        _event("убери манго"),
        infer_intent("убери манго"),
        state,
        _catalog(),
    )

    assert result.state.cart[0].status is ItemStatus.SKIPPED
    assert result.state.current_issue_item_id == ""
    assert result.state.current_issue_kind is None


def test_not_found_unrelated_product_phrase_is_ambiguous_without_mutation(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Случайная фраза, ошибочно оформленная как товар, не меняет draft."""
    state = _not_found_state()
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="ну ладно потом",
        items=[ExtractedItem(product_query="ну ладно потом")],
    )

    result = ConversationEngine(settings).handle(
        _event(command.text),
        command,
        state,
        _catalog(),
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].status is ItemStatus.NOT_FOUND
    assert result.state.cart[0].source_query == "манго"
    assert result.state.current_issue_item_id == "mango"


def test_not_found_product_add_details_cancel_keeps_existing_safe_flow(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Отмена запроса снабженцу продолжает существующий product-add flow."""
    state = _not_found_state(SessionStage.AWAIT_PRODUCT_ADD_DETAILS)
    state.pending_product_add_item_index = 0
    state.pending_product_add_request_id = "request-1"

    result = ConversationEngine(settings).handle(
        _event("нет, не отправляй этот запрос снабженцу"),
        infer_intent("нет, не отправляй этот запрос снабженцу"),
        state,
        _catalog(),
    )

    assert result.state.cart[0].status is ItemStatus.SKIPPED
    assert result.state.stage is SessionStage.REVIEW

