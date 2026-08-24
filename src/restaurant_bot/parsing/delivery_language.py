"""Распознаёт узкие формы пожеланий о времени и способе доставки."""

from __future__ import annotations

import re

from restaurant_bot.domain.text import clean_text, normalize_text

_DELIVERY_WISH_RE = re.compile(
    r"\b(?:привезти|доставить|привези|привезите|доставь|доставьте)\b"
    r"|\bчтобы\s+(?:привезли|доставили)\b",
    flags=re.IGNORECASE,
)


def has_delivery_wish_shape(text: str) -> bool:
    """Проверяет, похожа ли фраза на инструкцию о желаемой доставке."""
    return bool(_DELIVERY_WISH_RE.search(normalize_text(text)))


def extract_delivery_wish(text: str) -> str:
    """Возвращает только явно произнесённую инструкцию о доставке."""
    source = clean_text(text)
    match = re.search(
        r"(?:\b(?:все|всё)(?:\s+это\s+дело)?\s+)?"
        r"(?P<wish>(?:привез\w*|достав\w*|чтобы\s+(?:привезли|доставили))\b[^.!?]*)",
        source,
        flags=re.IGNORECASE,
    )
    return match.group("wish").strip(" ,;:-—–") if match else ""
