"""Создаёт идентификаторы внешних запросов на добавление товара."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from restaurant_bot.domain.models import CartItem


def new_product_add_request_id(item: CartItem | None) -> str:
    """Создаёт устойчивый идентификатор запроса на новый товар."""
    item_part = (
        "item"
        if item is None
        else "".join(ch for ch in item.id if ch.isalnum() or ch in "_-")[:24] or "item"
    )
    return f"add-{int(datetime.now(UTC).timestamp() * 1000):x}-{item_part}-{secrets.token_hex(3)}"
