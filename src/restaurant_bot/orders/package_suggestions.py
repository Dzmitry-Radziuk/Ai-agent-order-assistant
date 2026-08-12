"""Рассчитывает информационную рекомендацию числа каталожных упаковок."""

from __future__ import annotations

import math
import re

from restaurant_bot.domain.models import CartItem
from restaurant_bot.domain.unit_conversion import convert_quantity
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit


def package_count_suggestion(item: CartItem) -> tuple[int, float, str] | None:
    """Предлагает число упаковок по весу или объёму в названии."""
    if (
        item.quantity is None
        or item.unit not in {"г", "кг", "мл", "л"}
        or item.catalog_unit not in {"шт", "уп", "кор", "пач", "бан", "бут"}
    ):
        return None

    unit_pattern = "|".join(
        sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
    )
    for match in re.finditer(
        rf"(\d+(?:[,.]\d+)?)\s*({unit_pattern})\b",
        item.catalog_name or item.source_query,
        flags=re.I,
    ):
        package_quantity = float(match.group(1).replace(",", "."))
        package_unit = normalize_unit(match.group(2))
        converted_package = convert_quantity(package_quantity, package_unit, item.unit)
        if converted_package is None or converted_package <= 0:
            continue
        count = max(1, math.ceil(item.quantity / converted_package))
        return count, round(count * converted_package, 6), item.unit
    return None
