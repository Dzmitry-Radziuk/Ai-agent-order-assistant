"""Содержит чистые запросы к состоянию диалога."""

from restaurant_bot.domain.models import CartItem, ConversationState, ItemStatus

_UNRESOLVED_PRIORITY = {
    ItemStatus.DUPLICATE_PENDING: 0,
    ItemStatus.UNIT_MISMATCH: 1,
    ItemStatus.MISSING_QTY: 2,
    ItemStatus.AMBIGUOUS: 3,
    ItemStatus.NOT_FOUND: 4,
    ItemStatus.NEW: 5,
    ItemStatus.AI_PENDING: 6,
}


def first_unresolved(state: ConversationState) -> CartItem | None:
    """Возвращает приоритетную нерешённую позицию без изменения состояния."""
    unresolved = [item for item in state.cart if item.status in _UNRESOLVED_PRIORITY]
    unresolved.sort(key=lambda item: _UNRESOLVED_PRIORITY[item.status])
    return unresolved[0] if unresolved else None


def item_index(state: ConversationState, item: CartItem) -> int:
    """Возвращает безопасный индекс позиции в текущем черновике."""
    return next(
        (index for index, candidate in enumerate(state.cart) if candidate.id == item.id),
        0,
    )
