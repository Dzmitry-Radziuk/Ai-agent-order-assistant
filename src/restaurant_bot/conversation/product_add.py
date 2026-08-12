"""Изменяет channel-neutral временный контекст product-add."""

from __future__ import annotations

from restaurant_bot.domain.models import ConversationState


def clear_product_add_pending(state: ConversationState) -> None:
    """Очищает временное состояние запроса нового товара."""
    state.pending_product_add_item_index = None
    state.pending_product_add_request_id = ""
    state.product_add_write_in_progress = False
