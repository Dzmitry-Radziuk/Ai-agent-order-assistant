"""Содержит детерминированный разбор числовых значений."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from restaurant_bot.domain.text import clean_text


def to_float(value: Any) -> float | None:
    """Безопасно преобразует значение в положительное число."""
    if value is None or value == "":
        return None
    raw = clean_text(value).replace(" ", "").replace(",", ".")
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if raw.count(".") > 1:
        sign = "-" if raw.startswith("-") else ""
        parts = raw.lstrip("-").split(".")
        # Google Sheets может добавлять к российской валюте букву и точку.
        # Последние одна-две цифры считаются десятичной частью, предыдущие
        # точки — визуальными разделителями тысяч.
        if parts[-1] and len(parts[-1]) <= 2:
            raw = sign + "".join(parts[:-1]) + "." + parts[-1]
        else:
            raw = sign + "".join(parts)
    try:
        number = float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return None
    return number if number > 0 else None
