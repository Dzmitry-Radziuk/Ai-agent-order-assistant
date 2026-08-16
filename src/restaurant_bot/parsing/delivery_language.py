"""Распознаёт узкие формы пожеланий о времени и способе доставки."""

from __future__ import annotations

import re

from restaurant_bot.domain.text import normalize_text

_DELIVERY_WISH_RE = re.compile(
    r"\b(?:привезти|доставить|привези|привезите|доставь|доставьте)\b"
    r"|\bчтобы\s+(?:привезли|доставили)\b",
    flags=re.IGNORECASE,
)


def has_delivery_wish_shape(text: str) -> bool:
    """Проверяет, похожа ли фраза на инструкцию о желаемой доставке."""
    return bool(_DELIVERY_WISH_RE.search(normalize_text(text)))
