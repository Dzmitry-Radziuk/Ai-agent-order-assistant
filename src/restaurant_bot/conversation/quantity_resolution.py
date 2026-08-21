"""Содержит независимые от канала правила кратности и предупреждений количества."""

from __future__ import annotations

import math

from restaurant_bot.domain.models import CartItem, ConversationState, ItemStatus


def nearest_valid_multiple(quantity: float, multiple: float | None) -> float | None:
    """Возвращает ближайшее допустимое кратное для количества новой заявки."""
    if not multiple or multiple <= 0:
        return None
    ratio = quantity / multiple
    nearest = math.ceil(ratio - 1e-9) * multiple
    if abs(nearest - quantity) < 1e-9:
        return None
    return round(nearest, 6)


def suggested_quantity_for_multiple(item: CartItem) -> float | None:
    """Предлагает ближайшее кратное для количества новой заявки."""
    if item.quantity is None:
        return None
    return nearest_valid_multiple(item.quantity, item.minimum_multiple)


def is_multiple_warning(item: CartItem) -> bool:
    """Проверяет, требует ли позиция выбора количества по кратности."""
    return (
        item.status == ItemStatus.MATCHED
        and item.suggested_quantity is not None
        and item.suggested_quantity != item.quantity
    )


def multiple_warnings(state: ConversationState) -> list[CartItem]:
    """Возвращает предупреждения о кратности в порядке черновика."""
    return [item for item in state.cart if is_multiple_warning(item)]


def select_multiple_warning(state: ConversationState) -> CartItem | None:
    """Выбирает текущую или первую позицию с предупреждением о кратности."""
    current = state.current_item()
    warnings = multiple_warnings(state)
    if current in warnings:
        return current
    if warnings:
        state.current_issue_item_id = warnings[0].id
        return warnings[0]
    return None
