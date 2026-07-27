from restaurant_bot.domain.models import CartItem, ConversationState, ItemStatus
from restaurant_bot.services.engine import ConversationEngine


def test_current_issue_uses_state_item_id_before_cart_order(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что текущий уточнение использует состояние позиция идентификатор до черновик заказ."""
    engine = ConversationEngine(settings)
    first = CartItem(id="first", source_query="Первый", status=ItemStatus.NOT_FOUND)
    current = CartItem(id="current", source_query="Текущий", status=ItemStatus.MISSING_QTY)
    state = ConversationState(cart=[first, current], current_issue_item_id="current")

    assert engine._callback_item_index(type("Command", (), {"callback_target": ""})(), state) == 1


def test_unresolved_priority_handles_duplicate_before_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что неразрешённая priority handles дубликат до количество."""
    engine = ConversationEngine(settings)
    duplicate = CartItem(id="duplicate", source_query="Дубль", status=ItemStatus.DUPLICATE_PENDING)
    missing = CartItem(id="missing", source_query="Количество", status=ItemStatus.MISSING_QTY)

    assert engine._first_unresolved(ConversationState(cart=[missing, duplicate])).id == "duplicate"
