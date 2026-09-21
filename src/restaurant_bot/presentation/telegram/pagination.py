"""Хранит размеры страниц Telegram-представления."""

from __future__ import annotations

PRODUCTS_PAGE_SIZE = 10
CART_PAGE_SIZE = PRODUCTS_PAGE_SIZE
FINAL_REVIEW_PAGE_SIZE = PRODUCTS_PAGE_SIZE


def page_count(item_count: int, page_size: int) -> int:
    """Возвращает количество страниц для заданного числа элементов."""
    if page_size <= 0:
        raise ValueError("Размер страницы должен быть положительным")
    return max(1, (item_count + page_size - 1) // page_size)
