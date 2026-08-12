"""Содержит физическое преобразование совместимых единиц."""

from __future__ import annotations

from restaurant_bot.domain.units import normalize_unit


def convert_quantity(quantity: float, source_unit: str, target_unit: str) -> float | None:
    """Преобразует количество между совместимыми единицами."""
    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    if source == target or not source or not target:
        return quantity
    factors = {
        ("г", "кг"): 0.001,
        ("кг", "г"): 1000,
        ("мл", "л"): 0.001,
        ("л", "мл"): 1000,
    }
    factor = factors.get((source, target))
    return quantity * factor if factor else None
