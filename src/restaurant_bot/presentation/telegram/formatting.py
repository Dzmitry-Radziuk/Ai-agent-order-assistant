"""Формирует Telegram-представление «formatting»."""

from __future__ import annotations

import html
import re
from typing import Any

from restaurant_bot.domain.text import clean_text

_STATUS_TECHNICAL_PREFIX_RE = re.compile(r"^\s*Вручную\s*\|\s*", re.IGNORECASE)


def escape(value: Any) -> str:
    """Экранирует текст для безопасного HTML Telegram."""
    return html.escape(clean_text(value), quote=False)


def product_name(value: Any) -> str:
    """Форматирует название товара жирным безопасным HTML."""
    return f"<b>{escape(value)}</b>"


def heading(value: Any) -> str:
    """Форматирует заголовок карточки жирным подчёркнутым HTML."""
    return f"<b><u>{escape(value)}</u></b>"


def format_status(value: Any) -> str:
    """Убирает только известный технический префикс статуса для Telegram."""
    status = clean_text(value).strip()
    if not status:
        return "Статус не указан"
    status = _STATUS_TECHNICAL_PREFIX_RE.sub("", status).strip()
    return status.rstrip(".").strip() or "Статус не указан"


def format_number(value: float | None) -> str:
    """Форматирует число для сообщения пользователю."""
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def russian_plural(
    count: int,
    one: str,
    few: str,
    many: str,
) -> str:
    """Выбирает русскую форму существительного после числа."""
    last_two = count % 100
    last = count % 10
    if last == 1 and last_two != 11:
        return one
    if last in {2, 3, 4} and last_two not in {12, 13, 14}:
        return few
    return many
