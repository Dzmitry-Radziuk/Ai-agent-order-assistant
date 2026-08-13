"""Содержит чистые правила кодов приглашения заведений."""

from __future__ import annotations

import re

from restaurant_bot.domain.text import clean_text


def normalize_code(value: str) -> str:
    """Нормализует код приглашения."""
    return clean_text(value).upper()


def valid_code(value: str) -> bool:
    """Проверяет формат кода приглашения."""
    code = normalize_code(value)
    return bool(re.fullmatch(r"[A-ZА-ЯЁ0-9]{4,32}", code) and re.search(r"\d", code))
