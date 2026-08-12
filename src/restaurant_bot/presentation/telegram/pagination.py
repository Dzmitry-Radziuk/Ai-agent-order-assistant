"""Хранит размеры страниц Telegram-представления."""

from __future__ import annotations

CART_PAGE_SIZE = 20
FINAL_REVIEW_PAGE_SIZE = 20


def page_count(item_count: int, page_size: int) -> int:
    """Возвращает количество страниц для заданного числа элементов."""
    if page_size <= 0:
        raise ValueError("Размер страницы должен быть положительным")
    return max(1, (item_count + page_size - 1) // page_size)
