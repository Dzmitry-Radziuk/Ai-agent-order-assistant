"""Регрессионные сценарии прерывания открытого выбора кандидата."""

from __future__ import annotations

import pytest

from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
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


def _candidate_state() -> ConversationState:
    """Создаёт черновик с открытым выбором кандидата для сыра."""
    item = CartItem(
        id="cheese",
        source_query="сыр",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(product_id="parmesan", name="Сыр Пармезан", unit="кг"),
            Candidate(product_id="gouda", name="Сыр Гауда", unit="кг"),
            Candidate(product_id="cheddar", name="Сыр Чеддер", unit="кг"),
        ],
    )
    return ConversationState(cart=[item], current_issue_item_id=item.id)


def _event(text: str, input_type: InputKind = InputKind.TEXT) -> TelegramEvent:
    """Создаёт нормализованное событие для теста маршрутизации."""
    return TelegramEvent(update_id=1, chat_id="1", input_type=input_type, text=text)


def _catalog() -> list[CatalogProduct]:
    """Возвращает каталог кандидатов и новых товаров сценария."""
    return [
        CatalogProduct(product_id="parmesan", name="Пармезан", unit="кг"),
        CatalogProduct(product_id="gouda", name="Сыр Гауда", unit="кг"),
        CatalogProduct(product_id="cheddar", name="Сыр Чеддер", unit="кг"),
        CatalogProduct(product_id="dill", name="Укроп", unit="кг"),
    ]


def test_new_product_does_not_select_matching_old_candidate(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что конкретный ADD_ITEMS прерывает старый candidate context."""
    state = _candidate_state()
    result = ConversationEngine(settings).handle(
        _event("пармезан 3 кг"),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text="пармезан 3 кг",
            items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
        ),
        state,
        _catalog(),
    )

    assert len(result.state.cart) == 2
    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert [candidate.product_id for candidate in result.state.cart[0].candidates] == [
        "parmesan",
        "gouda",
        "cheddar",
    ]
    assert result.state.cart[1].source_query == "пармезан"
    assert result.state.cart[1].quantity == 3
    assert result.state.cart[1].unit == "кг"


@pytest.mark.parametrize("input_type", [InputKind.TEXT, InputKind.VOICE])
def test_new_product_with_voice_or_text_keeps_old_candidate_context(
    settings, input_type: InputKind
) -> None:  # type: ignore[no-untyped-def]
    """Проверяет одинаковое прерывание candidate context для текста и голоса."""
    state = _candidate_state()
    text = "пармезан три килограмма" if input_type is InputKind.VOICE else "пармезан 3 кг"
    command_text = text
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text=command_text,
        items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
    )
    result = ConversationEngine(settings).handle(
        _event(text, input_type),
        command,
        state,
        _catalog(),
    )

    assert len(result.state.cart) == 2
    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert result.state.cart[0].candidates[0].product_id == "parmesan"
    assert result.state.cart[1].source_query == "пармезан"
    assert result.state.cart[1].quantity == 3


def test_unique_candidate_name_can_continue_context(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что однозначное имя кандидата остаётся contextual selection."""
    state = _candidate_state()
    result = ConversationEngine(settings).handle(
        _event("гауда"),
        infer_intent("гауда"),
        state,
        _catalog(),
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].status is ItemStatus.MISSING_QTY
    assert result.state.cart[0].catalog_product_id == "gouda"


def test_show_cart_interrupts_candidate_fallback(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что навигация не выбирает кандидата по словам команды."""
    state = _candidate_state()
    result = ConversationEngine(settings).handle(
        _event("покажи черновик"),
        infer_intent("покажи черновик"),
        state,
        _catalog(),
    )

    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert result.state.cart[0].catalog_product_id == ""


def test_explicit_add_leadin_interrupts_candidate_fallback(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что явная просьба добавить новый товар не выбирает старый вариант."""
    state = _candidate_state()
    text = "добавь укроп 2 кг"
    result = ConversationEngine(settings).handle(
        _event(text),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=text,
            items=[ExtractedItem(product_query="укроп", quantity=2, unit="кг")],
        ),
        state,
        _catalog(),
    )

    assert len(result.state.cart) == 2
    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert result.state.cart[1].source_query == "укроп"
    assert result.state.cart[1].quantity == 2


def test_remove_ambiguous_item_clears_current_issue_reference(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет удаление старой позиции без битой ссылки на candidate context."""
    state = _candidate_state()
    result = ConversationEngine(settings).handle(
        _event("убери сыр"),
        infer_intent("убери сыр"),
        state,
        _catalog(),
    )

    assert result.state.cart[0].status is ItemStatus.SKIPPED
    assert result.state.current_issue_item_id == ""
    assert result.state.current_issue_kind is None


def test_thanks_does_not_select_candidate(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что благодарность не меняет открытый выбор кандидата."""
    state = _candidate_state()
    result = ConversationEngine(settings).handle(
        _event("спасибо"),
        infer_intent("спасибо"),
        state,
        _catalog(),
    )

    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert result.state.cart[0].catalog_product_id == ""


def test_unrelated_phrase_keeps_candidate_context_unchanged(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет безопасное поведение для фразы без надёжного выбора."""
    state = _candidate_state()
    result = ConversationEngine(settings).handle(
        _event("ну посмотрим"),
        ParsedCommand(intent=Intent.UNKNOWN, text="ну посмотрим"),
        state,
        _catalog(),
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert result.state.cart[0].catalog_product_id == ""
    assert [candidate.product_id for candidate in result.state.cart[0].candidates] == [
        "parmesan",
        "gouda",
        "cheddar",
    ]


def test_invalid_candidate_name_does_not_choose_first_candidate(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет безопасный отказ от имени, которого нет среди кандидатов."""
    state = _candidate_state()
    result = ConversationEngine(settings).handle(
        _event("говядина"),
        ParsedCommand(
            intent=Intent.SELECT_CANDIDATE,
            text="говядина",
            selection_query="говядина",
        ),
        state,
        _catalog(),
    )

    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert result.state.cart[0].catalog_product_id == ""
