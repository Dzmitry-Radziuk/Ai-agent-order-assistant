from __future__ import annotations

import html
from typing import Any

from restaurant_bot.text_normalization import clean_text


def escape(value: Any) -> str:
    """Экранирует текст для безопасного HTML Telegram."""
    return html.escape(clean_text(value), quote=False)


def format_number(value: float | None) -> str:
    """Форматирует число для сообщения пользователю."""
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")
