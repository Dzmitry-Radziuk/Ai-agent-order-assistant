"""Проверяет поведение, связанное с модулем «test draft actions»."""

from copy import deepcopy

import pytest

from restaurant_bot.conversation.draft_actions import (
    DraftActionOutcome,
    confirm_duplicate_item,
    remove_item,
    skip_current_item,
)
from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    DepartmentQuantities,
    ExtractedItem,
    ItemStatus,
    ParsedCommand,
)


def test_skip_current_item_preserves_progression_inputs() -> None:
    """Пропуск меняет только ожидаемый статус и счётчик новых позиций."""
    item = CartItem(id="one", source_query="Курица", status=ItemStatus.MISSING_QTY)
    state = ConversationState(
        cart=[item],
        current_issue_item_id=item.id,
        pending_added_items_count=2,
    )

    result = skip_current_item(state)

    assert result.outcome is DraftActionOutcome.ADVANCE
    assert item.status is ItemStatus.SKIPPED
    assert state.current_issue_item_id == ""
    assert state.pending_added_items_count == 1


def test_confirm_duplicate_keeps_unit_mismatch_unmerged() -> None:
    """Подтверждение дубликата не сливает позиции с разными единицами."""
    existing = CartItem(
        id="existing",
        source_query="Молоко",
        catalog_product_id="milk",
        quantity=2,
        unit="шт",
    )
    duplicate = CartItem(
        id="duplicate",
        source_query="Молоко",
        catalog_product_id="milk",
        quantity=3,
        unit="кг",
        catalog_unit="л",
        issue_message="existing",
        status=ItemStatus.DUPLICATE_PENDING,
    )
    state = ConversationState(cart=[existing, duplicate], current_issue_item_id=duplicate.id)
    before = deepcopy(state.model_dump())

    result = confirm_duplicate_item(state)

    assert result.outcome is DraftActionOutcome.ADVANCE
    assert state.model_dump() == before


@pytest.mark.parametrize("second_assigned", [True, False])
def test_duplicate_confirmation_preserves_allocations_or_requires_selection(
    second_assigned: bool,
) -> None:
    """Суммирует распределения двух фото и не оставляет старые отделы для новой суммы."""
    existing = CartItem(
        id="first",
        source_query="Молоко",
        quantity=2,
        status=ItemStatus.MATCHED,
        department_quantities=DepartmentQuantities(hall=2),
    )
    duplicate = CartItem(
        id="second",
        source_query="Молоко",
        quantity=3,
        status=ItemStatus.DUPLICATE_PENDING,
        issue_message=existing.id,
        department_quantities=DepartmentQuantities(bar=3)
        if second_assigned
        else DepartmentQuantities(),
    )
    state = ConversationState(cart=[existing, duplicate], current_issue_item_id=duplicate.id)
    confirm_duplicate_item(state)
    assert existing.quantity == 5
    assert duplicate.status is ItemStatus.SKIPPED
    assert existing.department_quantities == (
        DepartmentQuantities(hall=2, bar=3) if second_assigned else DepartmentQuantities()
    )
    assert existing.has_department_assignment() is second_assigned
    assert state.department_confirmation_required and not state.department_confirmed
    confirm_duplicate_item(state)
    assert existing.quantity == 5


def test_remove_item_reports_active_and_pending_outcomes() -> None:
    """Удаление различает активную позицию и pending comment item."""
    active = CartItem(id="active", source_query="Курица")
    pending = ExtractedItem(product_query="Пармезан")
    state = ConversationState(
        cart=[active],
        current_issue_item_id=active.id,
        pending_comment_items=[pending],
    )

    removed_active = remove_item(ParsedCommand(target_query="курица"), state)
    removed_pending = remove_item(ParsedCommand(target_query="пармезан"), state)

    assert removed_active.outcome is DraftActionOutcome.REMOVED
    assert active.status is ItemStatus.SKIPPED
    assert removed_pending.outcome is DraftActionOutcome.REMOVED
    assert state.pending_comment_items == []


def test_remove_item_does_not_mutate_for_unknown_target() -> None:
    """Неизвестная позиция возвращает безопасный исход без мутации state."""
    state = ConversationState(cart=[CartItem(id="one", source_query="Курица")])
    before = deepcopy(state.model_dump())

    result = remove_item(ParsedCommand(target_query="укроп"), state)

    assert result.outcome is DraftActionOutcome.NOT_FOUND
    assert state.model_dump() == before
