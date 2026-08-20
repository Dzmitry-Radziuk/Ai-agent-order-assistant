"""Распознаёт вопросы пользователя о текущем заведении и привязке."""

from __future__ import annotations

import re

from restaurant_bot.domain.text import normalize_text

_VENUE_WORD_RE = re.compile(
    r"\b(?:заведени\w*|ресторан\w*|кафе\w*|точк\w*|мест\w*|организаци\w*|"
    r"привяз\w*|подключ\w*|закреп\w*|аккаунт\w*)\b"
)
_VENUE_RELATION_RE = re.compile(r"\b(?:связан\w*|работа\w*|труд\w*|служ\w*|закрепл\w*|выбран\w*)\b")
_IDENTITY_WORD_RE = re.compile(
    r"\b(?:каком|какая|какое|какие|какую|где|кто|какой|"
    r"текущ\w*|сейчас|(?:мо|мо[её]го|мо[её]му|мо[её]й|мо[иими])\b|"
    r"у\s+меня|подключен\w*|подключён\w*|привязан\w*|"
    r"закрепл\w*|выбран\w*|покаж\w*|скажи\w*|узна\w*|расскаж\w*)\b"
)
_EXPLICIT_ORDER_RE = re.compile(
    r"\b(?:добав\w*|закаж\w*|куп\w*|внес\w*|полож\w*|удал\w*|"
    r"отправ\w*|оформ\w*|товар\w*|продукт\w*|количеств\w*|"
    r"\d+(?:[,.]\d+)?\s*(?:кг|г|л|мл|шт|штук|упак\w*))\b"
)


def is_venue_status_query(text: str) -> bool:
    """Определяет вопрос о заведении, не создавая товарную команду."""
    normalized = normalize_text(text)
    if not normalized or _EXPLICIT_ORDER_RE.search(normalized):
        return False
    if _VENUE_WORD_RE.search(normalized):
        return bool(
            _IDENTITY_WORD_RE.search(normalized)
            or _VENUE_RELATION_RE.search(normalized)
            or "к как" in normalized
        )
    return bool(
        re.search(
            r"\b(?:где|каком|какая|какое|какие)\b.*\b(?:я|мы)\b.*"
            r"\b(?:работа\w*|труд\w*|служ\w*)\b",
            normalized,
        )
    )
