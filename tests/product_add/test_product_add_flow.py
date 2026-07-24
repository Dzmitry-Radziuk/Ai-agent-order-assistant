import re

from restaurant_bot.domain.models import CartItem, ConversationState
from restaurant_bot.services.product_add_flow import (
    clear_product_add_pending,
    new_product_add_request_id,
    product_add_prompt,
)


def test_new_product_add_request_id_is_safe_and_contains_item_id() -> None:
    """Идентификатор запроса безопасен для хранения и сохраняет связь с товаром."""
    item = CartItem(id="item / №42", source_query="Креветки")

    request_id = new_product_add_request_id(item)

    assert re.fullmatch(r"add-[0-9a-f]+-item42-[0-9a-f]{6}", request_id)


def test_product_add_prompt_contains_query_and_user_guidance() -> None:
    """Подсказка объясняет повару, что именно нужно написать о товаре."""
    item = CartItem(id="item-1", source_query="Креветки королевские")

    text = product_add_prompt(item)

    assert "Опишите товар одним сообщением" in text
    assert "название, бренд, фасовку" in text
    assert "Товар: Креветки королевские" in text


def test_clear_product_add_pending_preserves_cart_and_completed_requests() -> None:
    """Очистка временного шага не удаляет корзину и сохранённые запросы."""
    item = CartItem(id="item-1", source_query="Креветки")
    state = ConversationState(
        cart=[item],
        pending_product_add_item_index=0,
        pending_product_add_request_id="add-1",
        product_add_write_in_progress=True,
        product_add_requests=[{"request_id": "add-old", "status": "submitted"}],
    )

    clear_product_add_pending(state)

    assert state.pending_product_add_item_index is None
    assert state.pending_product_add_request_id == ""
    assert state.product_add_write_in_progress is False
    assert state.cart == [item]
    assert state.product_add_requests == [{"request_id": "add-old", "status": "submitted"}]
