"""Содержит переходы и нормализацию страниц состояния диалога."""

from restaurant_bot.domain.models import ConversationState, ItemStatus


def normalize_cart_page(state: ConversationState) -> int:
    """Нормализует и сохраняет страницу черновика после изменения его состава."""
    active_count = sum(item.status != ItemStatus.SKIPPED for item in state.cart)
    total_pages = max(1, (active_count + 20 - 1) // 20)
    state.cart_page = min(max(0, state.cart_page), total_pages - 1)
    return state.cart_page
