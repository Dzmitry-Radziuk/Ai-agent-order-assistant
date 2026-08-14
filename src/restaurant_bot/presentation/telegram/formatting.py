"""Формирует Telegram-представление «formatting»."""

from __future__ import annotations

import html
from typing import Any

from restaurant_bot.domain.text import clean_text


def escape(value: Any) -> str:
    """Экранирует текст для безопасного HTML Telegram."""
    return html.escape(clean_text(value), quote=False)


def product_name(value: Any) -> str:
    """Форматирует название товара жирным безопасным HTML."""
    return f"<b>{escape(value)}</b>"


def heading(value: Any) -> str:
    """Форматирует заголовок карточки жирным подчёркнутым HTML."""
    return f"<b><u>{escape(value)}</u></b>"


def format_number(value: float | None) -> str:
    """Форматирует число для сообщения пользователю."""
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")
