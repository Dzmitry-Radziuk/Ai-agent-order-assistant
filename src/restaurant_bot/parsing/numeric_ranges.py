"""Находит диапазоны, являющиеся характеристиками товара."""

from __future__ import annotations

import re
from typing import Any

from restaurant_bot.domain.text import clean_text
from restaurant_bot.domain.units import UNIT_ALIASES
from restaurant_bot.parsing.number_words import NUMBER_WORDS


def numeric_range_spans(value: Any) -> list[tuple[int, int]]:
    """Находит цифровые и словесные диапазоны характеристик товара."""
    text = clean_text(value)
    if not text:
        return []
    unit_pattern = "|".join(
        sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
    )
    pattern = re.compile(
        rf"(?<!\w)\d+(?:[,.]\d+)?\s*(?:--|[-–—]|/|на|x|х)\s*\d+(?:[,.]\d+)?"
        rf"(?:\s*(?:{unit_pattern}))?\b",
        flags=re.IGNORECASE,
    )
    spans = [match.span() for match in pattern.finditer(text)]
    number_word = "|".join(
        sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True)
    )
    word_phrase = rf"(?:{number_word})(?:\s+(?:{number_word}))*"
    spoken_pattern = re.compile(
        rf"(?<!\w){word_phrase}\s*[-–—]\s*{word_phrase}"
        rf"(?:\s*(?:{unit_pattern}))?\b",
        flags=re.IGNORECASE,
    )
    spans.extend(match.span() for match in spoken_pattern.finditer(text))
    return sorted(spans)
