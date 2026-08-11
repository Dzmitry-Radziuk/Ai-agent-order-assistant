"""Содержит операции целостности и безопасного слияния черновика."""

from __future__ import annotations

from restaurant_bot.conversation.comments import merge_comments
from restaurant_bot.domain.models import CartItem, CommentSource, ConversationState, ItemStatus
from restaurant_bot.services.text import normalize_unit


def has_active_draft_items(state: ConversationState) -> bool:
    """Проверяет наличие позиций, которые пользователь ещё может потерять."""
    return any(item.status != ItemStatus.SKIPPED for item in state.cart)


def find_duplicate(state: ConversationState, item: CartItem) -> CartItem | None:
    """Находит дубликат товарной позиции."""
    if not item.catalog_product_id:
        return None
    return next(
        (
            existing
            for existing in state.cart
            if existing.status != ItemStatus.SKIPPED
            and existing.catalog_product_id == item.catalog_product_id
        ),
        None,
    )


def remove_exact_cart_duplicates(state: ConversationState) -> None:
    """Объединяет подтверждённые дубликаты позиций черновика."""
    owners: dict[tuple[str, str, str], CartItem] = {}
    duplicate_ids: set[str] = set()
    for item in state.cart:
        # Ожидающий duplicate — это карточка подтверждения, но не шум извлечения.
        # Объединяются только уже подтверждённые строки.
        if not item.catalog_product_id or item.status != ItemStatus.MATCHED:
            continue
        key = (
            item.catalog_product_id,
            normalize_unit(item.unit or item.catalog_unit),
            item.department,
        )
        owner = owners.get(key)
        if owner is None:
            owners[key] = item
            continue
        # Равные количества обычно означают повтор одной операции или дубль
        # распознавания, поэтому они не удваиваются.
        if owner.quantity != item.quantity:
            owner.quantity = (owner.quantity or 0) + (item.quantity or 0)
        owner.comment = merge_comments(owner.comment, item.comment)
        if item.comment and owner.comment_source is CommentSource.NONE:
            owner.comment_source = item.comment_source
        duplicate_ids.add(item.id)

    if not duplicate_ids:
        return
    state.cart = [item for item in state.cart if item.id not in duplicate_ids]
    if state.current_issue_item_id in duplicate_ids:
        state.current_issue_item_id = ""
