"""Определяет доменные правила и данные «text»."""

from __future__ import annotations

import re
from typing import Any


def clean_text(value: Any) -> str:
    """Очищает произвольное текстовое значение."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_text(value: Any) -> str:
    """Нормализует текст для сравнения."""
    text = clean_text(value).lower().replace("ё", "е")
    text = re.sub(r"[«»\"'`]", "", text)
    text = re.sub(r"[^a-zа-я0-9%.,/\-\s]", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()
