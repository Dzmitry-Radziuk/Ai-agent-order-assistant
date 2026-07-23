from __future__ import annotations

import secrets
from datetime import UTC, datetime

from restaurant_bot.domain.models import CartItem, ConversationState


def new_product_add_request_id(item: CartItem | None) -> str:
    """Создаёт устойчивый идентификатор запроса на новый товар."""
    item_part = (
        "item"
        if item is None
        else "".join(ch for ch in item.id if ch.isalnum() or ch in "_-")[:24] or "item"
    )
    return f"add-{int(datetime.now(UTC).timestamp() * 1000):x}-{item_part}-{secrets.token_hex(3)}"


def product_add_prompt(item: CartItem) -> str:
    """Формирует понятный запрос подробного описания нового товара."""
    return (
        "✏️ <b>Опишите товар одним сообщением</b>\n\n"
        "Чтобы менеджеру по снабжению было легче найти этот товар, опишите его максимально подробно: "
        "название, бренд, фасовку или объём и другие важные детали.\n\n"
        "Например: <code>Мисо-паста Genzo, 1 кг</code> или "
        "<code>Краб камчатский М/Л, 6 кг</code>.\n\n"
        f"Товар: {item.source_query}"
    )


def clear_product_add_pending(state: ConversationState) -> None:
    """Очищает временное состояние запроса нового товара."""
    state.pending_product_add_item_index = None
    state.pending_product_add_request_id = ""
    state.product_add_write_in_progress = False
