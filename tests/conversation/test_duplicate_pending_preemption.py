"""Проверяет поведение, связанное с модулем «test duplicate pending preemption»."""

from __future__ import annotations

import pytest

from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
)
from restaurant_bot.conversation.routing.state_compatibility import (
    StateCompatibilityPolicy,
)
from restaurant_bot.domain.models import (
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


def _event(text: str, input_type: InputKind = InputKind.TEXT) -> TelegramEvent:
    """Создаёт событие для проверки маршрутизации дубликата."""
    return TelegramEvent(update_id=1, chat_id="1", input_type=input_type, text=text)


def _catalog() -> list[CatalogProduct]:
    """Возвращает каталог для новых позиций сценария."""
    return [
        CatalogProduct(product_id="parmesan", name="Пармезан", unit="кг"),
        CatalogProduct(product_id="dill", name="Укроп", unit="кг"),
    ]


def _duplicate_state() -> ConversationState:
    """Создаёт draft с активным duplicate prompt."""
    existing = CartItem(
        id="existing",
        source_query="Сыр",
        catalog_product_id="cheese",
        catalog_name="Сыр",
        catalog_unit="кг",
        quantity=2,
        unit="кг",
        status=ItemStatus.MATCHED,
    )
    duplicate = CartItem(
        id="duplicate",
        source_query="Сыр",
        catalog_product_id="cheese",
        catalog_name="Сыр",
        catalog_unit="кг",
        quantity=3,
        unit="кг",
        status=ItemStatus.DUPLICATE_PENDING,
        issue_message=existing.id,
        duplicate_existing_quantity=existing.quantity or 0,
        duplicate_existing_unit=existing.unit,
    )
    return ConversationState(cart=[existing, duplicate], current_issue_item_id=duplicate.id)


def _new_item_command(text: str, query: str = "Пармезан") -> ParsedCommand:
    """Создаёт concrete ADD_ITEMS для независимой новой позиции."""
    return ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text=text,
        items=[ExtractedItem(product_query=query, quantity=3, unit="кг")],
    )


def test_duplicate_policy_interrupts_concrete_add_items() -> None:
    """Прерывает поток дубликата для конкретной новой товарной позиции."""
    command = _new_item_command("Пармезан 3 кг")
    decision = StateCompatibilityPolicy().evaluate(
        command,
        _duplicate_state(),
        CompatibilityContext.DUPLICATE_PENDING,
    )

    assert decision.action is CompatibilityAction.INTERRUPT


@pytest.mark.parametrize("input_type", [InputKind.TEXT, InputKind.VOICE])
def test_concrete_add_items_keeps_old_duplicate_context(settings, input_type: InputKind) -> None:  # type: ignore[no-untyped-def]
    """Добавляет новый товар отдельно для текста и голоса."""
    text = "Пармезан 3 кг" if input_type is InputKind.TEXT else "Пармезан три килограмма"
    result = ConversationEngine(settings).handle(
        _event(text, input_type),
        _new_item_command(text),
        _duplicate_state(),
        _catalog(),
    )

    assert len(result.state.cart) == 3
    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert result.state.cart[1].status is ItemStatus.DUPLICATE_PENDING
    assert result.state.cart[1].quantity == 3
    assert result.state.cart[1].issue_message == "existing"
    assert result.state.current_issue_item_id == "duplicate"
    assert result.state.cart[2].source_query == "Пармезан"
    assert result.state.cart[2].quantity == 3
    assert result.state.cart[2].unit == "кг"
    assert result.state.cart[2].comment == ""
    assert result.state.cart[2].issue_message == ""
    assert result.state.cart[2].duplicate_existing_quantity == 0
    assert result.state.cart[2].duplicate_existing_unit == ""
    assert [candidate.product_id for candidate in result.state.cart[2].candidates] == ["parmesan"]


@pytest.mark.parametrize(
    ("intent", "text"),
    [
        (Intent.SHOW_CART, "Покажи черновик"),
        (Intent.THANKS, "Спасибо"),
    ],
)
def test_independent_navigation_does_not_apply_duplicate_context(
    settings, intent: Intent, text: str
) -> None:  # type: ignore[no-untyped-def]
    """Независимая команда не подтверждает и не изменяет duplicate."""
    state = _duplicate_state()
    result = ConversationEngine(settings).handle(
        _event(text),
        ParsedCommand(intent=intent, text=text),
        state,
        _catalog(),
    )

    assert [item.status for item in result.state.cart] == [
        ItemStatus.MATCHED,
        ItemStatus.DUPLICATE_PENDING,
    ]
    assert result.state.current_issue_item_id == "duplicate"


def test_duplicate_random_add_items_is_safe_and_does_not_mutate_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Неопределённый товарный fallback повторяет запрос о дубликате без изменения состояния."""
    state = _duplicate_state()
    decision = StateCompatibilityPolicy().evaluate(
        ParsedCommand(intent=Intent.UNKNOWN, text="Ну ладно потом"),
        state,
        CompatibilityContext.DUPLICATE_PENDING,
    )
    before = [(item.status, item.quantity, item.unit) for item in state.cart]
    result = ConversationEngine(settings).handle(
        _event("Ну ладно потом"),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text="Ну ладно потом",
            items=[ExtractedItem(product_query="Ну ладно потом")],
        ),
        state,
        _catalog(),
    )

    assert decision.action is CompatibilityAction.AMBIGUOUS
    assert [(item.status, item.quantity, item.unit) for item in result.state.cart] == before
    assert result.state.current_issue_item_id == "duplicate"


def test_duplicate_short_quantity_continues_existing_flow(settings) -> None:  # type: ignore[no-untyped-def]
    """Короткий ответ количеством объединяет текущий duplicate."""
    state = _duplicate_state()
    result = ConversationEngine(settings).handle(
        _event("3 кг"),
        ParsedCommand(intent=Intent.UNKNOWN, text="3 кг"),
        state,
        _catalog(),
    )

    assert result.state.cart[0].quantity == 5
    assert result.state.cart[1].status is ItemStatus.SKIPPED
    assert result.state.current_issue_item_id == ""


def test_remove_current_duplicate_without_target_clears_issue_reference(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаление текущего duplicate не оставляет битый modal focus."""
    result = ConversationEngine(settings).handle(
        _event("Убери"),
        ParsedCommand(intent=Intent.REMOVE_ITEM, text="Убери"),
        _duplicate_state(),
        _catalog(),
    )

    assert result.state.cart[1].status is ItemStatus.SKIPPED
    assert result.state.current_issue_item_id == ""
    assert result.state.current_issue_kind is None


def test_remove_named_equal_rows_preserves_ambiguous_target_behavior(settings) -> None:  # type: ignore[no-untyped-def]
    """Не выбирает произвольную строку при равном target score."""
    state = _duplicate_state()
    result = ConversationEngine(settings).handle(
        _event("Убери сыр"),
        ParsedCommand(intent=Intent.REMOVE_ITEM, target_query="сыр", text="Убери сыр"),
        state,
        _catalog(),
    )

    assert [item.status for item in result.state.cart] == [
        ItemStatus.MATCHED,
        ItemStatus.DUPLICATE_PENDING,
    ]
    assert result.state.current_issue_item_id == "duplicate"
