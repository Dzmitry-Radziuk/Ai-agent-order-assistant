"""Содержит переходы и нормализацию страниц состояния диалога."""

from restaurant_bot.conversation.comments import clear_pending_comment
from restaurant_bot.conversation.product_add import clear_product_add_pending
from restaurant_bot.domain.models import ConversationState, ItemStatus, SearchScope


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


def clear_transient_dialog_state(state: ConversationState) -> None:
    """Очищает временный контекст диалога без изменения постоянного черновика."""
    state.current_issue_item_id = ""
    state.current_issue_kind = None
    state.issue_context_stack = []
    state.search_scope = SearchScope.SUPPLIER_ONLY
    state.supplier_search_locked = False
    state.supplier_hint_context = ""
    state.manual_item_index = None
    state.unit_item_index = None
    state.edit_multiple_index = None
    state.pending_added_items_count = 0
    clear_pending_comment(state)
    clear_product_add_pending(state)
