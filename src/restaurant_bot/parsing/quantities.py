"""Содержит детерминированные primitives разбора короткого количества."""

from __future__ import annotations

import re

from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.number_words import NUMBER_WORDS, parse_number_words
from restaurant_bot.parsing.numeric_ranges import numeric_range_spans
from restaurant_bot.text_normalization import normalize_text

_EXPLICIT_ORDER_QUANTITY_RE = re.compile(
    r"(?:мне\s+)?(?:нужн(?:о|а|ы)|надо|закаж(?:и|ем|у)|добав(?:ь|ить)|"
    r"постав(?:ь|ить)|возьм(?:и|ем)|количеств(?:о|ом)?|вес)\b",
    flags=re.I,
)


def has_explicit_order_quantity(source_line: str, quantity: float | None) -> bool:
    """Отличает объём заказа от числа в размере или фасовке товара."""
    if quantity is None:
        return False
    source = str(source_line or "").strip()
    if not source:
        return False
    range_spans = numeric_range_spans(source)
    masked = list(source)
    for start, end in range_spans:
        masked[start:end] = [" "] * (end - start)
    masked_source = "".join(masked)
    unit_pattern = "|".join(
        sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
    )
    number_word_pattern = "|".join(
        sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True)
    )
    value_pattern = re.compile(
        rf"(?<![\w-])(?P<value>\d+(?:[,.]\d+)?|"
        rf"(?:{number_word_pattern})(?:\s+(?:{number_word_pattern}))*)"
        rf"\s*(?P<unit>{unit_pattern})?\b",
        flags=re.I,
    )
    values: list[float] = []
    for match in value_pattern.finditer(masked_source):
        raw_value = match.group("value").casefold()
        try:
            value = float(raw_value.replace(",", "."))
        except ValueError:
            tokens = raw_value.split()
            parsed = parse_number_words(tokens, 0)
            value = parsed[0] if parsed and parsed[1] == len(tokens) else -1
        if value >= 0:
            values.append(value)
    if not values or not any(abs(value - quantity) <= 1e-9 for value in values):
        return False
    if range_spans:
        return True
    if has_explicit_order_marker(masked_source):
        return True
    if len(values) > 1:
        return True
    return bool(
        re.search(
            rf"(?:^|[-—–:])\s*\d+(?:[,.]\d+)?\s*(?:{unit_pattern})?\s*$",
            masked_source,
            flags=re.I,
        )
    )


def has_explicit_order_marker(source_line: str) -> bool:
    """Проверяет наличие словесного маркера количества заказа."""
    return bool(_EXPLICIT_ORDER_QUANTITY_RE.search(str(source_line or "")))


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
