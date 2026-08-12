"""Содержит каноническую агрегацию минимальных сумм поставщиков."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from restaurant_bot.domain.models import CartItem, ConversationState, ItemStatus


@dataclass(frozen=True, slots=True)
class SupplierMinimumWarning:
    """Описывает недобор минимальной суммы одного поставщика."""

    supplier: str
    current_amount: float
    added_amount: float
    minimum_amount: float
    items: tuple[CartItem, ...]

    @property
    def missing_amount(self) -> float:
        """Возвращает сумму, которой не хватает до минимального заказа."""
        return self.minimum_amount - self.current_amount - self.added_amount


class _SupplierMinimumGroup(TypedDict):
    """Хранит промежуточные итоги по одному поставщику."""

    current: float
    added: float
    minimum: float
    items: list[CartItem]


def supplier_minimum_warnings(state: ConversationState) -> list[SupplierMinimumWarning]:
    """Возвращает предупреждения поставщиков в порядке их появления в черновике."""
    groups: dict[str, _SupplierMinimumGroup] = {}
    for item in state.cart:
        if item.status != ItemStatus.MATCHED or not item.supplier:
            continue
        group = groups.setdefault(
            item.supplier,
            {"current": 0.0, "added": 0.0, "minimum": 0.0, "items": []},
        )
        group["current"] = max(group["current"], float(item.supplier_current_sum or 0))
        group["added"] += item.amount
        group["minimum"] = max(group["minimum"], float(item.supplier_minimum_amount or 0))
        group["items"].append(item)

    warnings: list[SupplierMinimumWarning] = []
    for supplier, group in groups.items():
        current = group["current"]
        added = group["added"]
        minimum = group["minimum"]
        if minimum > 0 and current + added < minimum:
            warnings.append(
                SupplierMinimumWarning(
                    supplier=supplier,
                    current_amount=current,
                    added_amount=added,
                    minimum_amount=minimum,
                    items=tuple(group["items"]),
                )
            )
    return warnings
