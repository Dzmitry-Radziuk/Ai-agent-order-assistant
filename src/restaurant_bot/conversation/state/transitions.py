"""Содержит переходы и нормализацию страниц состояния диалога."""

from restaurant_bot.domain.models import ConversationState, ItemStatus


def normalize_cart_page(state: ConversationState, *, page_size: int) -> int:
    """Нормализует и сохраняет страницу черновика после изменения его состава."""
    active_count = sum(item.status != ItemStatus.SKIPPED for item in state.cart)
    if page_size <= 0:
        raise ValueError("Размер страницы должен быть положительным")
    total_pages = max(1, (active_count + page_size - 1) // page_size)
    state.cart_page = min(max(0, state.cart_page), total_pages - 1)
    return state.cart_page


def normalize_final_review_page(state: ConversationState, *, page_size: int) -> int:
    """Нормализует и сохраняет страницу финальной проверки заявки."""
    active_count = sum(item.status != ItemStatus.SKIPPED for item in state.cart)
    if page_size <= 0:
        raise ValueError("Размер страницы должен быть положительным")
    total_pages = max(1, (active_count + page_size - 1) // page_size)
    state.final_review_page = min(max(0, state.final_review_page), total_pages - 1)
    return state.final_review_page
