"""Содержит детерминированные primitives разбора короткого количества."""

from __future__ import annotations

import re

from restaurant_bot.services.text import (
    UNIT_ALIASES,
    normalize_text,
    normalize_unit,
    parse_number_words,
)


def _is_standalone_quantity(value: str) -> bool:
    """Проверяет, что фрагмент содержит только количество и единицу."""
    tokens = [
        token.strip(" .,:;—–-")
        for token in normalize_text(value).split()
        if token.strip(" .,:;—–-")
    ]
    parsed = parse_number_words(tokens, 0)
    if parsed is None:
        return False
    _, end = parsed
    if end < len(tokens) and tokens[end] in UNIT_ALIASES:
        end += 1
    return end == len(tokens)


def parse_quantity_unit(text: str) -> tuple[float | None, str]:
    """Разбирает короткий ответ с количеством и единицей."""
    normalized = normalize_text(text)
    if not normalized:
        return None, ""
    tokens = [
        cleaned
        for token in normalized.replace(",", ".").split()
        if (cleaned := re.sub(r"\.+$", "", token))
    ]
    if len(tokens) >= 1:
        try:
            quantity = float(tokens[0])
            unit = (
                normalize_unit(tokens[1]) if len(tokens) > 1 and tokens[1] in UNIT_ALIASES else ""
            )
            return quantity, unit
        except ValueError:
            pass
    parsed = parse_number_words(tokens, 0)
    if parsed:
        quantity, end = parsed
        unit = (
            normalize_unit(tokens[end]) if end < len(tokens) and tokens[end] in UNIT_ALIASES else ""
        )
        return quantity, unit
    return None, ""
